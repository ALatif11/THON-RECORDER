"""
Transcribes audio to timestamped segments using faster-whisper (runs locally).

Requires: pip install faster-whisper
"""

import json
import os
import re
import wave
from pathlib import Path

# CUDA libraries live in cuda_libs/ next to this package. They must be made
# loadable BEFORE faster-whisper is imported, because ctranslate2 resolves them
# at import time. Doing it here means no PATH edit and no admin rights.
CUDA_LIB_DIR = Path(__file__).resolve().parent.parent / "cuda_libs"


def _register_cuda_libs():
    """Make the bundled CUDA DLLs loadable. Returns the folder used, or None."""
    if os.name != "nt" or not CUDA_LIB_DIR.is_dir():
        return None
    folder = str(CUDA_LIB_DIR)
    try:
        os.add_dll_directory(folder)
    except (OSError, AttributeError):
        return None
    # add_dll_directory covers explicit LoadLibrary calls, but some dependent
    # DLLs are resolved through the process PATH, so cover that too.
    if folder not in os.environ.get("PATH", ""):
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
    return folder


CUDA_LIBS_REGISTERED = _register_cuda_libs()

try:
    from faster_whisper import WhisperModel

    AVAILABLE = True
    IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - optional heavy dependency
    WhisperModel = None
    AVAILABLE = False
    IMPORT_ERROR = str(exc)

_model_cache = {}

MODEL_SIZES = ["auto", "tiny.en", "base.en", "small.en", "medium.en", "large-v3"]

# "auto" is large-v3 on a GPU and small.en on a CPU, where large-v3 runs
# slower than the meeting itself.
DEFAULT_MODEL = "auto"


def _size_for(model_size, device):
    if model_size != "auto":
        return model_size
    return "large-v3" if device == "cuda" else "small.en"


class TranscriptionUnavailable(RuntimeError):
    """Raised when faster-whisper is not installed."""


def _get_model(size, device, compute_type, report):
    key = (size, device, compute_type)
    if key not in _model_cache:
        report(f"Loading whisper '{size}' on {device}...", None)
        _model_cache[key] = WhisperModel(size, device=device, compute_type=compute_type)
    return _model_cache[key]


def _reporter(progress):
    """
    Normalize the optional progress callback.

    The callback gets (message: str, pct: float | None). We always also print,
    so the CLI keeps behaving the way it did before.
    """

    def report(message, pct, end="\n"):
        print(f"\r{message}" if end == "" else message, end=end, flush=True)
        if progress is not None:
            try:
                progress(message, pct)
            except Exception:
                pass  # a broken progress sink must never kill the transcription

    return report


def audio_duration(audio_path):
    """Length of a wav in seconds, or None if it can't be read cheaply."""
    try:
        with wave.open(str(audio_path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return None


def transcribe(audio_path, model_size=DEFAULT_MODEL, device="auto", cache=True, progress=None):
    """
    Transcribe an audio file. Returns a list of
    {"start": float, "end": float, "text": str}.

    Caches to <audio>.transcript.json so you never pay for the same audio twice.

    device: "cuda" if you have an NVIDIA GPU, "cpu" otherwise, "auto" to guess.
    progress: optional callable(message, pct) for live UI updates.
    """
    report = _reporter(progress)
    audio_path = Path(audio_path)
    cache_path = audio_path.with_suffix(".transcript.json")

    if cache and cache_path.exists():
        report(f"Using cached transcript: {cache_path.name}", 100)
        return json.loads(cache_path.read_text(encoding="utf-8"))

    if not AVAILABLE:
        raise TranscriptionUnavailable(
            "faster-whisper is not installed, so audio cannot be transcribed "
            f"({IMPORT_ERROR}). Install it with: pip install faster-whisper"
        )

    device, compute_type = detect_device(device)
    model, device, compute_type = load_model(model_size, device, compute_type, report)

    report(f"Transcribing {audio_path.name}...", 0)
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        vad_filter=True,  # drops silence, big speedup on lectures with dead air
        vad_parameters={"min_silence_duration_ms": 700},
    )

    total = info.duration or audio_duration(audio_path) or 0

    out = []
    for seg in segments:
        out.append(
            {"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()}
        )
        mins, secs = int(seg.end // 60), int(seg.end % 60)
        pct = min(99.0, round(seg.end / total * 100, 1)) if total else None
        report(f"  transcribed through {mins:02d}:{secs:02d}", pct, end="")

    report(f"\nDone. {len(out)} segments, {total / 60:.1f} min of audio.", 100)

    if cache:
        cache_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    return out


def to_text(segments, with_timestamps=True):
    """Flatten segments into a plain string for feeding to an LLM."""
    lines = []
    for s in segments:
        if with_timestamps:
            ts = f"[{int(s['start'] // 60):02d}:{int(s['start'] % 60):02d}]"
            lines.append(f"{ts} {s['text']}")
        else:
            lines.append(s["text"])
    return "\n".join(lines)


def chunk(segments, window_minutes=15, overlap_minutes=2):
    """
    Split segments into overlapping time windows. Overlap keeps a concept
    that straddles a boundary from getting cut in half.
    """
    if not segments:
        return []

    window = window_minutes * 60
    overlap = overlap_minutes * 60
    step = max(window - overlap, 60)  # never allow a zero/negative stride
    total = segments[-1]["end"]

    chunks = []
    start = 0.0
    while start < total:
        end = start + window
        body = [s for s in segments if s["start"] < end and s["end"] > start]
        if body:
            chunks.append(body)
        start += step

    return chunks


# --------------------------------------------------------------- device

# CTranslate2 ships its own CUDA runtime, so torch has no say in whether
# faster-whisper can use the GPU. Asking torch (the original approach) meant
# that with no torch installed the answer was always "cpu", regardless of the
# hardware actually present.


def cuda_device_count():
    """How many GPUs CTranslate2 can actually see. 0 means CPU only."""
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


def supported_compute_types(device):
    try:
        import ctranslate2

        return set(ctranslate2.get_supported_compute_types(device))
    except Exception:
        return set()


def detect_device(device="auto"):
    """
    Resolve ("auto" | "cuda" | "cpu") into a concrete (device, compute_type).

    The compute type is checked against what this GPU actually supports, so an
    older card that cannot do float16 falls back instead of failing at load.
    """
    if device == "auto":
        device = "cuda" if cuda_device_count() > 0 else "cpu"

    supported = supported_compute_types(device)
    preferences = (
        ("float16", "int8_float16", "bfloat16", "float32")
        if device == "cuda"
        else ("int8", "float32")
    )
    for candidate in preferences:
        if not supported or candidate in supported:
            return device, candidate
    return device, "float32"


def diagnostics():
    """Everything needed to explain why transcription lands on CPU or GPU."""
    info = {
        "faster_whisper": AVAILABLE,
        "faster_whisper_error": IMPORT_ERROR,
        "ctranslate2_version": None,
        "cuda_device_count": 0,
        "device": None,
        "compute_type": None,
        "supported_cpu": sorted(supported_compute_types("cpu")),
        "supported_cuda": [],
    }
    try:
        import ctranslate2

        info["ctranslate2_version"] = ctranslate2.__version__
    except Exception:
        pass

    info["cuda_device_count"] = cuda_device_count()
    if info["cuda_device_count"]:
        info["supported_cuda"] = sorted(supported_compute_types("cuda"))
        info["cuda_libraries"] = check_cuda_libraries()
        info["cuda_lib_dir"] = CUDA_LIBS_REGISTERED
        info["cuda_usable"] = all(info["cuda_libraries"].values())
    else:
        info["cuda_libraries"] = {}
        info["cuda_lib_dir"] = CUDA_LIBS_REGISTERED
        info["cuda_usable"] = False

    info["device"], info["compute_type"] = detect_device("auto")
    return info


# The CUDA libraries CTranslate2 loads at model-load time. get_cuda_device_count
# only talks to the driver, so it happily reports a GPU on a machine where these
# are missing, and the failure only surfaces when a model is loaded.
CUDA_LIBS_WINDOWS = ["cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll"]
CUDA_LIBS_LINUX = ["libcublas.so.12", "libcublasLt.so.12", "libcudnn.so.9"]

_cuda_failure = None


def check_cuda_libraries():
    """
    Try to load each required CUDA library by name, cheaply.

    Returns {"name": True/False}. Loading a DLL is far quicker than loading a
    whisper model, so this is safe to run from `doctor`.
    """
    import ctypes
    import platform

    windows = platform.system() == "Windows"
    names = CUDA_LIBS_WINDOWS if windows else CUDA_LIBS_LINUX
    loader = getattr(ctypes, "WinDLL", ctypes.CDLL) if windows else ctypes.CDLL

    results = {}
    for name in names:
        try:
            loader(name)
            results[name] = True
        except OSError:
            results[name] = False
    return results


def cuda_hint():
    """What to actually do about missing CUDA libraries, per platform."""
    import platform

    if platform.system() == "Windows":
        return (
            "CTranslate2 needs cuBLAS for CUDA 12 and cuDNN 9 for CUDA 12, which "
            "do not ship with the driver and are not installed by pip on Windows.\n"
            "The quickest fix is Purfview's prebuilt archive, which bundles exactly "
            "the right DLLs:\n"
            "  https://github.com/Purfview/whisper-standalone-win/releases/tag/libs\n"
            "Unzip it and either drop the DLLs beside your python.exe or add that "
            "folder to your PATH, then open a new terminal.\n"
            "Note it must be cuDNN 9, not 8. CTranslate2 dropped cuDNN 8 in 4.5.0."
        )
    return (
        "CTranslate2 needs cuBLAS for CUDA 12 and cuDNN 9 for CUDA 12. On Linux:\n"
        "  pip install nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*"
    )


def _summarize_cuda_error(exc):
    text = str(exc)
    match = re.search(r"(\S*(?:cublas|cudnn|cudart)\S*\.(?:dll|so[.\d]*))", text, re.I)
    if match:
        return f"{match.group(1)} could not be loaded"
    return text.split("\n")[0][:120]


def load_model(model_size, device, compute_type, report):
    """
    Load a model, falling back to CPU if the GPU libraries are not usable.

    A transcript produced slowly on the CPU beats no transcript at all, so a
    missing cuBLAS downgrades the run instead of killing it.

    Returns (model, device, compute_type).
    """
    global _cuda_failure
    try:
        return _get_model(_size_for(model_size, device), device, compute_type, report), device, compute_type
    except Exception as exc:
        if device != "cuda":
            raise
        _cuda_failure = str(exc)
        report(
            f"GPU unavailable: {_summarize_cuda_error(exc)}. Falling back to CPU, "
            "which is slower. Run `python run.py doctor` for how to fix it.",
            None,
        )
        device, compute_type = detect_device("cpu")
        return _get_model(_size_for(model_size, "cpu"), device, compute_type, report), device, compute_type
