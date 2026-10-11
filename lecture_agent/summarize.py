"""
Turns a meeting transcript into a draft announcement, fully on-device.

Two passes, both on a local Ollama model (nothing leaves the machine):
  1. MAP    - extract a fixed schema from each ~15 min chunk
  2. REDUCE - merge chunks and write the announcement draft

Requires Ollama running locally:  ollama pull llama3.2:3b
Override the model with OLLAMA_MODEL, the host with OLLAMA_HOST.
Put 2-3 past announcements in announcement_examples.txt to match the org's voice.
"""

import json
import os
import re
import urllib.error
import urllib.request

from .config import project_root
from .transcribe import chunk, to_text

AVAILABLE = True  # stdlib only
IMPORT_ERROR = None
FATAL = ()

MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

SCHEMA = """{
  "updates": [{"point": str, "timestamp": "MM:SS"}],
  "decisions": [{"decision": str, "timestamp": "MM:SS"}],
  "action_items": [{"task": str, "owner": str | null, "due": str | null, "timestamp": "MM:SS"}],
  "events": [{"name": str, "date": str | null, "time": str | null, "location": str | null, "timestamp": "MM:SS"}],
  "asks": [{"ask": str, "timestamp": "MM:SS"}]
}"""

MAP_PROMPT = """You are extracting notes from one segment of a student organization meeting transcript (a Penn State THON organization). The transcript came from speech recognition, so expect filler and mangled names.

Rules:
- Record only what was actually said. Empty arrays are correct when nothing fits.
- If someone corrects themselves, keep the corrected version.
- If a name, date, or place looks garbled, write your best guess and append " [?]".
- "asks" is anything members are asked to do (sign up, donate, show up, fill out a form).
- "action_items" are tasks assigned to a specific person or committee.

Timestamps are marked [MM:SS]. Respond with ONLY a JSON object matching this schema:

{schema}

TRANSCRIPT SEGMENT:
{text}"""

REDUCE_PROMPT = """You are writing the post-meeting announcement for a Penn State THON organization, from notes extracted from the meeting.

Rules:
- Merge duplicates. Do not add anything not in the notes.
- Keep every "[?]" flag so the president knows what to double-check.
- Lead with what members need to DO, then dates, then updates.
- Short, friendly, skimmable. Members read this on their phones.
- Plain text only. GroupMe does not render markdown: no ** or #. Use "-" for lists.
- Copy every date and time exactly as written in the notes. Never combine a weekday with a date the notes did not pair it with.
- The announcement is for members. Never add questions or notes to the president inside it.
- No em dashes.
{style}{tone}
Write only the announcement text, ready to post. Title: {title}

MEETING NOTES:
{notes}"""


# The Regenerate button's tone dropdown. "" keeps the default voice.
TONES = {
    "": "",
    "shorter": "- Make it noticeably shorter than usual: only what members must know or do.",
    "hype": "- Make it high energy and exciting. A few emojis are fine. FTK!",
    "formal": "- Make it polished and professional. No slang, no emojis.",
}


class SummarizationUnavailable(RuntimeError):
    """Raised when Ollama is not reachable."""


def _ollama(prompt, as_json=False):
    # Ollama's default context is 2-4K tokens and it silently drops the start
    # of a longer prompt, which is where the instructions are. A 17 minute
    # chunk is ~3.5K tokens of transcript alone. 8K costs ~1 GB of RAM on a 3B model.
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": 8192},
    }
    if as_json:
        body["format"] = "json"
    req = urllib.request.Request(
        f"{HOST}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read())["message"]["content"]
    except urllib.error.URLError as exc:
        raise SummarizationUnavailable(
            f"Could not reach Ollama at {HOST}. Start it and run `ollama pull {MODEL}`. ({exc})"
        ) from exc


def _parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def _style():
    path = project_root() / "announcement_examples.txt"
    examples = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    if not examples:
        return ""
    return "\nMatch the tone and format of these past announcements:\n\n" + examples + "\n"


def _reporter(progress):
    def report(message, pct):
        print(message)
        if progress is not None:
            try:
                progress(message, pct)
            except Exception:
                pass

    return report


def extract_chunk(segments):
    """MAP step: pull the schema out of one chunk."""
    return _parse_json(_ollama(MAP_PROMPT.format(schema=SCHEMA, text=to_text(segments)), as_json=True))


def summarize(segments, title="Meeting Recap", window_minutes=15, progress=None):
    """Run the full map-reduce. Returns (announcement_markdown, raw_notes)."""
    report = _reporter(progress)
    chunks = chunk(segments, window_minutes=window_minutes)
    if not chunks:
        raise RuntimeError("Nothing to summarize: the transcript is empty.")

    report(f"Extracting from {len(chunks)} chunks on {MODEL}...", 0)
    notes, failures = [], []
    for i, c in enumerate(chunks, 1):
        report(f"  chunk {i}/{len(chunks)}", round(i / (len(chunks) + 1) * 100, 1))
        try:
            notes.append(extract_chunk(c))
        except SummarizationUnavailable:
            raise  # Ollama down won't fix itself on chunk 2
        except Exception as e:
            failures.append(f"chunk {i}: {e}")
            report(f"    failed, skipping: {e}", None)

    if not notes:
        raise RuntimeError("Every chunk failed to extract. First error: " + (failures[0] if failures else "unknown"))

    report("Writing announcement...", 95)
    draft = write_announcement(notes, title)
    report("Done.", 100)
    return draft, notes


def write_announcement(notes, title="Meeting Recap", tone=""):
    """REDUCE step on its own, so Regenerate can rerun it without whisper."""
    prompt = REDUCE_PROMPT.format(
        style=_style(), tone=TONES.get(tone, ""), title=title, notes=json.dumps(notes, indent=2)
    )
    return _strip_preamble(_ollama(prompt))


def _strip_preamble(text):
    """
    Small models open with "Here is the announcement:" despite being told not
    to, and pad lists with bare "- [?]" lines that flag nothing.
    """
    text = re.sub(r"\A\s*(here('s| is)|sure)[^\n]*:\s*\n", "", text, flags=re.IGNORECASE)
    return re.sub(r"^[ \t]*[-*•]?[ \t]*\[\?\][ \t]*\n?", "", text, flags=re.MULTILINE).strip()


if __name__ == "__main__":
    # Self-check of the parsing that small local models make necessary.
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('Sure! {"a": [1]} hope that helps') == {"a": [1]}
    assert _strip_preamble("Here is the post-meeting announcement:\n\nHey team!") == "Hey team!"
    assert _strip_preamble("Here's your announcement:\nHey") == "Hey"
    assert _strip_preamble("Hey team! Here is the plan:\nCanning") == "Hey team! Here is the plan:\nCanning"
    assert _strip_preamble("Updates:\n- Raised $4,200\n- [?]\n* [?]\n- Meet at HUB [?]") == "Updates:\n- Raised $4,200\n- Meet at HUB [?]"
    print("ok")
