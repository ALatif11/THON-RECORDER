"""
THON Announcer.

Record a meeting, transcribe it locally, and draft an announcement for the
org's GroupMe.

Submodules are imported lazily. capture pulls in PyAudioWPatch (Windows only)
and transcribe pulls in faster-whisper, both of which are slow to import and
not always installed. Touching `lecture_agent.capture` still works; it just
happens on first use, so the web UI can start on a machine where one of them
is missing and report that cleanly instead of failing at import time.
"""

import importlib

__version__ = "0.2.0"

_SUBMODULES = {"capture", "transcribe", "summarize", "library", "pipeline", "config", "ingest"}

__all__ = sorted(_SUBMODULES)


def __getattr__(name):
    if name in _SUBMODULES:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | _SUBMODULES)
