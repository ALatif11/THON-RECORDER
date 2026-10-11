"""
FastAPI backend for the THON Announcer desktop app.

Everything is local: it binds to 127.0.0.1 and touches only the recordings/
and notes/ folders next to this file's parent.
"""

import html as html_mod
import os
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.jobs import Queues  # noqa: E402
from lecture_agent.library import Library  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="THON Announcer")
library = Library(root=ROOT)
queues = Queues(root=str(ROOT), recordings_dir=library.recordings_dir)

# Uploads land here first, then ingest moves them into recordings/. Kept inside
# the project so the move is same-volume, and out of recordings/ so a half
# written file never shows up in the library.
UPLOAD_TMP = ROOT / ".uploads"

# Read uploads in chunks. A 2 GB lecture video must never be held in memory.
UPLOAD_CHUNK = 1024 * 1024

_recorder = None


def recorder():
    """Created lazily so the server still boots without PyAudioWPatch."""
    global _recorder
    if _recorder is None:
        from lecture_agent.capture import Recorder

        _recorder = Recorder(out_dir=library.recordings_dir)
    return _recorder


# ---------------------------------------------------------------- markdown

try:
    import markdown as _md

    def render_markdown(text):
        return _md.markdown(text, extensions=["extra", "sane_lists", "toc"])

except ImportError:

    def render_markdown(text):
        # The `markdown` package is optional. Without it the notes are still
        # perfectly readable, just unstyled.
        return f"<pre class='raw-markdown'>{html_mod.escape(text)}</pre>"


# ---------------------------------------------------------------- models


class RecordStart(BaseModel):
    label: str = "meeting"
    source: str = "mic"  # "mic" or "speakers"
    device_index: int | None = None
    max_minutes: int = 180


class JobCreate(BaseModel):
    stem: str
    model_size: str = "auto"


# ---------------------------------------------------------------- routes


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    """One poll for everything the header and sidebar need."""
    from lecture_agent import capture, config, ingest, summarize, transcribe

    try:
        rec_status = recorder().status() if capture.AVAILABLE else _idle_status()
    except Exception as exc:
        rec_status = {**_idle_status(), "error": str(exc)}

    return {
        "capabilities": {
            "capture": capture.AVAILABLE,
            "capture_error": capture.IMPORT_ERROR,
            "transcribe": transcribe.AVAILABLE,
            "transcribe_error": transcribe.IMPORT_ERROR,
            "summarize": summarize.AVAILABLE,
            "api_key": True,  # notes run on local Ollama, no key needed
            "groupme": bool(config.load_bot_id()[0]),
            "markdown": "raw-markdown" not in render_markdown("_x_"),
        },
        "recording": rec_status,
        "jobs": queues.all(),
        "pending": library.pending(),
        "model_sizes": transcribe.MODEL_SIZES,
        "upload": {
            "available": ingest.AVAILABLE,
            "error": ingest.IMPORT_ERROR,
            "audio": sorted(ingest.AUDIO_EXTS),
            "video": sorted(ingest.VIDEO_EXTS),
        },
        "root": str(ROOT),
    }


def _idle_status():
    return {
        "recording": False,
        "label": None,
        "path": None,
        "device": None,
        "started_at": None,
        "elapsed": 0.0,
        "bytes": 0,
        "error": None,
    }


@app.get("/api/lectures")
def lectures():
    return {"lectures": library.lectures(), "labels": library.labels()}


@app.get("/api/lectures/{stem}")
def lecture(stem: str):
    stem = _safe_stem(stem)
    meta = library.describe(stem)
    if meta["status"] == "unknown":
        raise HTTPException(404, f"No meeting named {stem}")

    markdown_text = library.markdown(stem)
    return {
        "meta": meta,
        "markdown": markdown_text,
        "html": render_markdown(markdown_text) if markdown_text else None,
        "structured": library.structured(stem),
    }


class DraftSave(BaseModel):
    markdown: str


@app.put("/api/lectures/{stem}/notes")
def save_draft(stem: str, body: DraftSave):
    """The coordinator's edits to the announcement draft."""
    stem = _safe_stem(stem)
    library.notes_dir.mkdir(parents=True, exist_ok=True)
    library.markdown_path(stem).write_text(body.markdown, encoding="utf-8")
    return {"ok": True}


@app.post("/api/lectures/{stem}/post")
def post_draft(stem: str, body: DraftSave, dry_run: bool = False):
    """
    Save the draft, then post it to GroupMe.

    dry_run returns the exact messages that would go out, for the
    confirmation dialog, without posting anything.
    """
    from lecture_agent import groupme

    save_draft(stem, body)
    if not body.markdown.strip():
        raise HTTPException(400, "The announcement is empty.")
    try:
        if dry_run:
            groupme.require_bot_id()
            parts = groupme.split_message(body.markdown)
        else:
            parts = groupme.post(body.markdown)
            library.mark_posted(stem)
    except groupme.MissingBotId as exc:
        raise HTTPException(400, str(exc))
    except groupme.GroupMeError as exc:
        raise HTTPException(502, str(exc))
    return {"messages": parts, "posted_at": library.describe(stem)["posted_at"]}


class Regenerate(BaseModel):
    tone: str = ""


@app.post("/api/lectures/{stem}/regenerate")
def regenerate(stem: str, body: Regenerate):
    """Rerun only the final announcement step from the saved notes. No whisper."""
    from lecture_agent import summarize

    stem = _safe_stem(stem)
    notes = library.structured(stem)
    if not notes:
        raise HTTPException(404, "This meeting has no saved notes to rewrite from. Process it again.")
    try:
        draft = summarize.write_announcement(notes, title=stem.replace("_", " "), tone=body.tone)
    except summarize.SummarizationUnavailable as exc:
        raise HTTPException(503, str(exc))
    save_draft(stem, DraftSave(markdown=draft))
    return {"markdown": draft}


# ---------------------------------------------------------------- settings

EXAMPLES = ROOT / "announcement_examples.txt"


class Settings(BaseModel):
    bot_id: str | None = None  # None leaves the saved one alone
    examples: str | None = None


@app.get("/api/settings")
def get_settings():
    from lecture_agent import config

    bot_id, source = config.load_bot_id()
    return {
        "bot_id_set": bool(bot_id),
        "bot_id_hint": config.redact(bot_id) if bot_id else None,
        "bot_id_source": source,
        "examples": EXAMPLES.read_text(encoding="utf-8") if EXAMPLES.is_file() else "",
    }


@app.put("/api/settings")
def put_settings(body: Settings):
    from lecture_agent import config

    if body.bot_id is not None and body.bot_id.strip():
        config.save_bot_id(body.bot_id)
    if body.examples is not None:
        EXAMPLES.write_text(body.examples.strip(), encoding="utf-8")
    return get_settings()


@app.get("/api/lectures/{stem}/transcript")
def transcript(stem: str):
    stem = _safe_stem(stem)
    segments = library.transcript(stem)
    if segments is None:
        raise HTTPException(404, "No transcript for this meeting yet.")
    return {"stem": stem, "segments": segments}


@app.get("/api/search")
def search(q: str = "", limit: int = 200, sources: str = "notes,transcript"):
    wanted = tuple(s.strip() for s in sources.split(",") if s.strip())
    hits = library.search(q, limit=limit, sources=wanted)
    return {"query": q, "count": len(hits), "hits": hits}


@app.get("/api/devices")
def devices():
    from lecture_agent import capture

    return {"available": capture.AVAILABLE, "devices": capture.list_devices()}


@app.post("/api/record/start")
def record_start(body: RecordStart):
    from lecture_agent import capture

    try:
        return recorder().start(
            label=body.label,
            source=body.source,
            device_index=body.device_index,
            max_minutes=body.max_minutes,
        )
    except capture.CaptureUnavailable as exc:
        raise HTTPException(503, str(exc))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")


@app.post("/api/record/stop")
def record_stop(process: bool = True, model_size: str = "auto"):
    status = recorder().stop()
    path = status.get("path")

    # Straight from "stop" into the processing queue, which is the whole point
    # of having a queue: you close the laptop and the notes are there later.
    if process and path and Path(path).exists():
        job = queues.submit_process(Path(path).stem, path, model_size=model_size)
        status["job"] = job.as_dict()
    return status


@app.get("/api/record/status")
def record_status():
    from lecture_agent import capture

    if not capture.AVAILABLE:
        return _idle_status()
    return recorder().status()


@app.get("/api/jobs")
def jobs():
    return {"jobs": queues.all()}


@app.post("/api/jobs")
def create_job(body: JobCreate):
    stem = _safe_stem(body.stem)
    wav = library.wav_path(stem)
    if not wav.exists():
        raise HTTPException(404, f"No audio file for {stem}")
    return queues.submit_process(stem, wav, model_size=body.model_size).as_dict()


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: int):
    job = queues.cancel(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job.as_dict()


@app.post("/api/jobs/process-all")
def process_all(model_size: str = "auto"):
    created = []
    for item in library.pending():
        wav = library.wav_path(item["stem"])
        if wav.exists():
            created.append(
                queues.submit_process(item["stem"], wav, model_size=model_size).as_dict()
            )
    return {"queued": created}


@app.post("/api/upload")
async def upload(
    files: list[UploadFile] = File(...),
    label: str | None = Form(None),
    auto_queue: bool = Form(True),
):
    """
    Accept one or more audio or video files.

    Each file is streamed to disk in chunks, never buffered whole, then handed
    to the ingest queue which strips the audio out of it. Unsupported files are
    reported per file rather than failing the whole batch.
    """
    from lecture_agent import ingest

    if not ingest.AVAILABLE:
        raise HTTPException(503, f"PyAV is not installed: {ingest.IMPORT_ERROR}")

    UPLOAD_TMP.mkdir(parents=True, exist_ok=True)
    accepted, rejected = [], []

    for upload_file in files:
        name = Path(upload_file.filename or "upload").name
        if not name or not ingest.is_supported(name):
            rejected.append({"filename": name, "reason": f"{Path(name).suffix or 'no extension'} is not a supported audio or video type"})
            continue

        temp_path = UPLOAD_TMP / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"
        size = 0
        try:
            with open(temp_path, "wb") as out:
                while chunk := await upload_file.read(UPLOAD_CHUNK):
                    out.write(chunk)
                    size += len(chunk)
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            rejected.append({"filename": name, "reason": f"upload failed: {exc}"})
            continue
        finally:
            await upload_file.close()

        if size == 0:
            temp_path.unlink(missing_ok=True)
            rejected.append({"filename": name, "reason": "file was empty"})
            continue

        job = queues.submit_upload(temp_path, name, label=label, auto_queue=auto_queue)
        accepted.append({"filename": name, "bytes": size, "job": job.as_dict()})

    if not accepted and rejected:
        raise HTTPException(400, "; ".join(r["reason"] for r in rejected))

    return {"accepted": accepted, "rejected": rejected}


@app.get("/api/upload/formats")
def upload_formats():
    from lecture_agent import ingest

    return {
        "available": ingest.AVAILABLE,
        "audio": sorted(ingest.AUDIO_EXTS),
        "video": sorted(ingest.VIDEO_EXTS),
        "accept": ",".join(sorted(ingest.SUPPORTED_EXTS)),
    }


# ---------------------------------------------------------------- helpers


def _safe_stem(stem: str) -> str:
    """Stems come from the URL, so refuse anything that could walk the tree."""
    if not stem or "/" in stem or "\\" in stem or ".." in stem:
        raise HTTPException(400, "Bad meeting id")
    return stem


@app.exception_handler(Exception)
def unhandled(request, exc):  # pragma: no cover - safety net
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

queues.start()
