"""
The transcribe-then-summarize pipeline, shared by the CLI and the web UI.

Kept separate from run.py so the server does not have to import the argparse
entry point just to process a file.
"""

import json
from pathlib import Path

from . import summarize as summarize_mod
from . import transcribe as transcribe_mod
from .library import Library

# Transcription is the slow half, so it owns most of the progress bar.
TRANSCRIBE_SHARE = 0.7


def process(audio_path, model_size="auto", title=None, root=".", progress=None):
    """
    Transcribe and summarize one recording.

    Writes notes/<stem>.md and notes/<stem>.notes.json.
    Returns {"stem", "markdown_path", "json_path", "segments", "chunks"}.
    progress: optional callable(message, pct) where pct spans the whole job.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"No such file: {audio_path}")

    lib = Library(root=root)
    lib.notes_dir.mkdir(parents=True, exist_ok=True)

    stem = audio_path.stem
    title = title or stem.replace("_", " ")

    def stage(lo, hi):
        """Scale a stage's 0-100 into the overall bar."""

        def report(message, pct):
            if progress is None:
                return
            overall = None if pct is None else round(lo + (hi - lo) * pct / 100, 1)
            progress(message, overall)

        return report

    segments = transcribe_mod.transcribe(
        audio_path,
        model_size=model_size,
        progress=stage(0, TRANSCRIBE_SHARE * 100),
    )
    if not segments:
        raise RuntimeError(
            f"{audio_path.name} produced an empty transcript. "
            "The recording is probably silent. Check that the right microphone "
            "is set as the Windows default, and that it is not muted."
        )

    markdown, notes = summarize_mod.summarize(
        segments,
        title=title,
        progress=stage(TRANSCRIBE_SHARE * 100, 100),
    )

    md_path = lib.markdown_path(stem)
    md_path.write_text(markdown, encoding="utf-8")

    # Keep the structured version too. This is what search indexes, since
    # markdown is lossy for that.
    json_path = lib.notes_json_path(stem)
    json_path.write_text(json.dumps(notes, indent=2), encoding="utf-8")

    return {
        "stem": stem,
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "segments": len(segments),
        "chunks": len(notes),
    }
