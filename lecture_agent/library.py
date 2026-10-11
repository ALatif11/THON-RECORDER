"""
The semester library: what has been recorded, what has been processed, and
full text search across all of it.

A "lecture" is identified by its stem, the filename without extension:

    MATH451_2026-08-31_1400

which maps to up to four files:

    recordings/<stem>.wav                 the audio
    recordings/<stem>.transcript.json     cached ASR output
    notes/<stem>.md                       the readable overview
    notes/<stem>.notes.json               structured notes, what search indexes
    notes/<stem>.posted                   timestamp of the GroupMe post, if any
"""

import json
import re
from datetime import datetime
from pathlib import Path

# The trailing -N is the collision suffix ingest adds when two uploads land
# in the same minute. Without it those stems lose their date and sort last.
STEM_RE = re.compile(
    r"^(?P<label>.+?)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{3,4})(?:-(?P<seq>\d+))?$"
)

# Where structured notes hide their text, so search can point at real fields.
NOTE_FIELDS = {
    "updates": ("point",),
    "decisions": ("decision",),
    "action_items": ("task", "owner", "due"),
    "events": ("name", "date", "time", "location"),
    "asks": ("ask",),
}


class Library:
    def __init__(self, root=".", recordings="recordings", notes="notes"):
        self.root = Path(root)
        self.recordings_dir = self.root / recordings
        self.notes_dir = self.root / notes

    # -- paths ---------------------------------------------------------

    def wav_path(self, stem):
        return self.recordings_dir / f"{stem}.wav"

    def transcript_path(self, stem):
        return self.recordings_dir / f"{stem}.transcript.json"

    def markdown_path(self, stem):
        return self.notes_dir / f"{stem}.md"

    def notes_json_path(self, stem):
        return self.notes_dir / f"{stem}.notes.json"

    def posted_path(self, stem):
        return self.notes_dir / f"{stem}.posted"

    def mark_posted(self, stem):
        self.posted_path(stem).write_text(datetime.now().isoformat(timespec="minutes"), encoding="utf-8")

    # -- listing -------------------------------------------------------

    def stems(self):
        """Every stem we know about, from either directory."""
        found = set()
        if self.recordings_dir.is_dir():
            found |= {p.stem for p in self.recordings_dir.glob("*.wav")}
            # .transcript.json -> Path.stem leaves "<stem>.transcript"
            found |= {
                p.name[: -len(".transcript.json")]
                for p in self.recordings_dir.glob("*.transcript.json")
            }
        if self.notes_dir.is_dir():
            found |= {p.stem for p in self.notes_dir.glob("*.md")}
            found |= {
                p.name[: -len(".notes.json")] for p in self.notes_dir.glob("*.notes.json")
            }
        return found

    def describe(self, stem):
        """Everything the UI needs for one row in the lecture list."""
        wav = self.wav_path(stem)
        transcript = self.transcript_path(stem)
        md = self.markdown_path(stem)
        notes_json = self.notes_json_path(stem)

        match = STEM_RE.match(stem)
        if match:
            label = match.group("label")
            raw_time = match.group("time").zfill(4)
            when = f"{match.group('date')} {raw_time[:2]}:{raw_time[2:]}"
            seq = int(match.group("seq") or 0)
            sort_key = f"{match.group('date')}_{raw_time}_{seq:03d}"
        else:
            label = stem
            when = None
            sort_key = ""

        if md.exists():
            status = "noted"
        elif transcript.exists():
            status = "transcribed"
        elif wav.exists():
            status = "recorded"
        else:
            status = "unknown"

        size_mb = round(wav.stat().st_size / 1e6, 1) if wav.exists() else None
        modified = None
        for candidate in (md, transcript, wav):
            if candidate.exists():
                modified = datetime.fromtimestamp(candidate.stat().st_mtime).isoformat()
                break

        return {
            "stem": stem,
            "label": label,
            "when": when,
            "sort_key": sort_key,
            "status": status,
            "has_audio": wav.exists(),
            "has_transcript": transcript.exists(),
            "has_notes": md.exists(),
            "has_structured": notes_json.exists(),
            "size_mb": size_mb,
            "modified": modified,
            "posted_at": self.posted_path(stem).read_text(encoding="utf-8").strip()
            if self.posted_path(stem).exists()
            else None,
            "title": stem.replace("_", " "),
        }

    def lectures(self):
        """All lectures, newest first."""
        items = [self.describe(s) for s in self.stems()]
        items.sort(key=lambda d: (d["sort_key"], d["stem"]), reverse=True)
        return items

    def labels(self):
        """Distinct course labels, for filtering."""
        return sorted({item["label"] for item in self.lectures()})

    # -- reading -------------------------------------------------------

    def markdown(self, stem):
        path = self.markdown_path(stem)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def structured(self, stem):
        path = self.notes_json_path(stem)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def transcript(self, stem):
        path = self.transcript_path(stem)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def pending(self):
        """Lectures with audio but no notes yet, oldest first so a backlog drains in order."""
        items = [item for item in self.lectures() if item["has_audio"] and not item["has_notes"]]
        items.sort(key=lambda d: (d["sort_key"], d["stem"]))
        return items

    # -- search --------------------------------------------------------

    def search(self, query, limit=200, sources=("notes", "transcript")):
        """
        Full text search across the semester.

        All whitespace-separated terms must appear in a hit, case insensitive.
        Returns hits ordered newest lecture first, each with a timestamp when
        one is known so the UI can jump to the right moment.
        """
        terms = [t.lower() for t in query.split() if t.strip()]
        if not terms:
            return []

        def matches(text):
            low = text.lower()
            return all(t in low for t in terms)

        hits = []
        for item in self.lectures():
            stem = item["stem"]

            if "notes" in sources:
                for hit in self._search_structured(stem, matches):
                    hits.append({**hit, "lecture": item})
                if not item["has_structured"]:
                    for hit in self._search_markdown(stem, matches):
                        hits.append({**hit, "lecture": item})

            if "transcript" in sources:
                for hit in self._search_transcript(stem, matches):
                    hits.append({**hit, "lecture": item})

            if len(hits) >= limit:
                break

        return hits[:limit]

    def _search_structured(self, stem, matches):
        notes = self.structured(stem)
        if not notes:
            return []

        out = []
        # notes is a list of per-chunk dicts
        for chunk_notes in notes if isinstance(notes, list) else [notes]:
            if not isinstance(chunk_notes, dict):
                continue
            for category, fields in NOTE_FIELDS.items():
                for entry in chunk_notes.get(category) or []:
                    if not isinstance(entry, dict):
                        continue
                    blob = " ".join(str(entry.get(f) or "") for f in fields)
                    if matches(blob):
                        out.append(
                            {
                                "source": "notes",
                                "category": category,
                                "timestamp": entry.get("timestamp"),
                                "snippet": blob.strip(),
                            }
                        )
        return _dedupe(out)

    def _search_markdown(self, stem, matches):
        text = self.markdown(stem)
        if not text:
            return []
        out = []
        for line in text.splitlines():
            line = line.strip()
            if len(line) > 3 and matches(line):
                out.append(
                    {
                        "source": "notes",
                        "category": "markdown",
                        "timestamp": _first_timestamp(line),
                        "snippet": line.lstrip("#- *"),
                    }
                )
        return _dedupe(out)

    def _search_transcript(self, stem, matches):
        segments = self.transcript(stem)
        if not segments:
            return []
        out = []
        for seg in segments:
            text = seg.get("text", "")
            if matches(text):
                start = seg.get("start", 0)
                out.append(
                    {
                        "source": "transcript",
                        "category": "spoken",
                        "timestamp": f"{int(start // 60):02d}:{int(start % 60):02d}",
                        "seconds": start,
                        "snippet": text.strip(),
                    }
                )
        return out


def _first_timestamp(text):
    m = re.search(r"\b(\d{1,2}:\d{2})\b", text)
    return m.group(1) if m else None


def _dedupe(hits):
    """The map step overlaps chunks, so the same entry shows up more than once."""
    seen = set()
    out = []
    for hit in hits:
        key = (hit["source"], hit["snippet"][:160])
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out
