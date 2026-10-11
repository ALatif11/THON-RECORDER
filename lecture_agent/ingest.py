"""
Bringing existing audio and video files into the library.

Decoding uses PyAV, which faster-whisper already depends on and which bundles
the FFmpeg libraries. That means there is no separate ffmpeg to install, and
anything FFmpeg can open works here: mp4, mkv, mov, webm, mp3, m4a, and so on.

Video is never re-encoded. The video stream is simply ignored and only the
audio is decoded out, which is why "stripping" a large lecture recording takes
seconds rather than the minutes a transcode would.
"""

import re
import shutil
import wave
from datetime import datetime
from pathlib import Path

try:
    import av

    AVAILABLE = True
    IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - optional dependency
    av = None
    AVAILABLE = False
    IMPORT_ERROR = str(exc)

# PyAV renamed its base exception: AVError in the old releases, FFmpegError in
# the current ones. Bind whichever exists so this works on either.
if AVAILABLE:
    AV_ERROR = getattr(av, "FFmpegError", None) or getattr(av, "AVError", Exception)
else:  # pragma: no cover
    AV_ERROR = Exception

# Whisper resamples to 16 kHz mono internally, so writing anything richer just
# wastes disk and I/O for identical transcripts.
TARGET_RATE = 16000
TARGET_LAYOUT = "mono"
TARGET_FORMAT = "s16"

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".alac"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts"}
SUPPORTED_EXTS = AUDIO_EXTS | VIDEO_EXTS


class IngestError(RuntimeError):
    pass


def _require_av():
    if not AVAILABLE:
        raise IngestError(
            f"PyAV is not installed ({IMPORT_ERROR}), so uploaded files cannot be "
            "decoded. It normally arrives with faster-whisper. "
            "Install it with: pip install av"
        )


def is_supported(filename):
    return Path(filename).suffix.lower() in SUPPORTED_EXTS


def looks_like_video(filename):
    return Path(filename).suffix.lower() in VIDEO_EXTS


def safe_label(label, fallback="lecture"):
    cleaned = "".join(c for c in (label or "") if c.isalnum() or c in "-_")
    return cleaned or fallback


def label_from_filename(filename):
    """
    Guess a course label from a filename.

    "MATH451 lecture 3.mp4" -> "MATH451". Falls back to the whole stem, so an
    upload always gets something more useful than "lecture".
    """
    stem = Path(filename).stem
    match = re.match(r"([A-Za-z]{2,8}[\s_-]?\d{1,4}[A-Za-z]?)", stem.strip())
    if match:
        return safe_label(match.group(1).replace(" ", "").replace("-", ""))
    return safe_label(stem.replace(" ", "_"), fallback="lecture")


def build_stem(label, when=None):
    when = when or datetime.now()
    return f"{safe_label(label)}_{when.strftime('%Y-%m-%d_%H%M')}"


def unique_stem(recordings_dir, stem):
    """Never overwrite an existing recording; suffix instead."""
    recordings_dir = Path(recordings_dir)
    candidate = stem
    n = 2
    while (recordings_dir / f"{candidate}.wav").exists():
        candidate = f"{stem}-{n}"
        n += 1
    return candidate


def source_timestamp(path):
    """
    When the lecture happened, as best we can tell.

    Prefers the container's creation metadata, then the file's modification
    time, so an upload of last week's lecture sorts under last week rather
    than under today.
    """
    path = Path(path)
    if AVAILABLE:
        try:
            with av.open(str(path)) as container:
                raw = (container.metadata or {}).get("creation_time")
                if raw:
                    cleaned = raw.replace("Z", "+00:00")
                    return datetime.fromisoformat(cleaned).astimezone().replace(tzinfo=None)
        except Exception:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return datetime.now()


def probe(path):
    """Duration in seconds and whether there is a video stream. None if unknown."""
    _require_av()
    try:
        with av.open(str(path)) as container:
            duration = container.duration / av.time_base if container.duration else None
            has_audio = any(s.type == "audio" for s in container.streams)
            has_video = any(s.type == "video" for s in container.streams)
            return {"duration": duration, "has_audio": has_audio, "has_video": has_video}
    except AV_ERROR as exc:
        raise IngestError(f"Could not open {Path(path).name}: {exc}")


def extract_audio(src, dest_wav, progress=None, display_name=None):
    """
    Decode the audio stream of any media file into 16 kHz mono 16-bit wav.

    Video streams are skipped entirely rather than decoded and discarded.
    Returns the duration written, in seconds.
    """
    _require_av()
    src = Path(src)
    shown = display_name or src.name
    dest_wav = Path(dest_wav)
    dest_wav.parent.mkdir(parents=True, exist_ok=True)

    def report(message, pct):
        if progress is not None:
            try:
                progress(message, pct)
            except Exception:
                pass

    try:
        container = av.open(str(src))
    except AV_ERROR as exc:
        raise IngestError(f"Could not open {shown}: {exc}")

    with container:
        audio_streams = [s for s in container.streams if s.type == "audio"]
        if not audio_streams:
            raise IngestError(
                f"{shown} has no audio track. "
                "If this is a screen recording, check it was captured with audio."
            )

        stream = audio_streams[0]
        # Only decode audio. Without this PyAV also decodes the video frames,
        # which on a long lecture is the difference between seconds and minutes.
        stream.thread_type = "AUTO"

        total = container.duration / av.time_base if container.duration else None
        resampler = av.audio.resampler.AudioResampler(
            format=TARGET_FORMAT, layout=TARGET_LAYOUT, rate=TARGET_RATE
        )

        report(f"Extracting audio from {shown}", 0)

        written = 0
        with wave.open(str(dest_wav), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(TARGET_RATE)

            for frame in container.decode(stream):
                for resampled in resampler.resample(frame):
                    out.writeframes(resampled.to_ndarray().tobytes())
                    written += resampled.samples

                if total and written and (written // TARGET_RATE) % 30 == 0:
                    secs = written / TARGET_RATE
                    report(
                        f"Extracting audio, {int(secs // 60):02d}:{int(secs % 60):02d}",
                        min(99.0, round(secs / total * 100, 1)),
                    )

            # Flush whatever the resampler is still holding.
            for resampled in resampler.resample(None):
                out.writeframes(resampled.to_ndarray().tobytes())
                written += resampled.samples

    seconds = written / TARGET_RATE
    if seconds < 0.1:
        dest_wav.unlink(missing_ok=True)
        raise IngestError(f"{shown} decoded to an empty audio track.")

    report(f"Extracted {seconds / 60:.1f} min of audio", 100)
    return seconds


def ingest(src, recordings_dir, label=None, keep_original=True, progress=None,
           original_name=None):
    """
    Bring one uploaded file into the library.

    Writes recordings/<stem>.wav, and when keep_original is set also parks the
    source file beside it as recordings/<stem><original extension>.

    original_name is the filename the user actually uploaded. Uploads are
    streamed to a temp file with a generated name, so without this the label
    and every error message would quote a uuid instead of "MATH451 week 3.mp4".

    Returns {"stem", "wav_path", "original_path", "duration", "was_video"}.
    """
    src = Path(src)
    shown = original_name or src.name
    recordings_dir = Path(recordings_dir)
    recordings_dir.mkdir(parents=True, exist_ok=True)

    if not src.exists():
        raise IngestError(f"No such file: {shown}")
    if not is_supported(shown):
        raise IngestError(
            f"{Path(shown).suffix or 'That file type'} is not supported. "
            f"Audio: {', '.join(sorted(AUDIO_EXTS))}. "
            f"Video: {', '.join(sorted(VIDEO_EXTS))}."
        )

    label = safe_label(label) if label else label_from_filename(shown)
    stem = unique_stem(recordings_dir, build_stem(label, source_timestamp(src)))

    wav_path = recordings_dir / f"{stem}.wav"
    duration = extract_audio(src, wav_path, progress=progress, display_name=shown)

    original_path = None
    if keep_original:
        original_path = recordings_dir / f"{stem}{Path(shown).suffix.lower()}"
        try:
            shutil.move(str(src), str(original_path))
        except OSError:
            # Different volume, or the source is still held open. Copy instead.
            shutil.copy2(str(src), str(original_path))

    return {
        "stem": stem,
        "wav_path": str(wav_path),
        "original_path": str(original_path) if original_path else None,
        "duration": duration,
        "was_video": looks_like_video(shown),
    }
