"""
Records a meeting from one of two sources:

    "mic"       the default microphone, for an in-person meeting (the default)
    "speakers"  whatever is coming out of the speakers via WASAPI loopback,
                for Zoom or Teams. No virtual cable driver needed.

Two ways in:

    record(label="meeting")          blocking, for the CLI, stops on Ctrl+C
    Recorder().start(...) / .stop()  non-blocking, for the web UI

Requires: pip install PyAudioWPatch   (Windows only)
"""

import threading
import time
import wave
from datetime import datetime
from pathlib import Path

try:
    import pyaudiowpatch as pyaudio

    AVAILABLE = True
    IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - platform dependent
    pyaudio = None
    AVAILABLE = False
    IMPORT_ERROR = str(exc)

CHUNK = 1024


class CaptureUnavailable(RuntimeError):
    """Raised when audio capture cannot run on this machine."""


def _require_pyaudio():
    if not AVAILABLE:
        raise CaptureUnavailable(
            "PyAudioWPatch is not installed, so audio capture is "
            f"unavailable ({IMPORT_ERROR}). It only supports Windows. "
            "Install it with: pip install PyAudioWPatch"
        )


def _default_loopback_device(p):
    """Find the loopback device matching the current default output."""
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        raise CaptureUnavailable("WASAPI not available. Are you on Windows?")

    default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])

    # Loopback devices share the name of their output device
    for device in p.get_loopback_device_info_generator():
        if default_out["name"] in device["name"]:
            return device

    raise CaptureUnavailable(
        f"No loopback device found for '{default_out['name']}'. "
        "Try changing your default playback device in Windows sound settings."
    )


def _default_mic_device(p):
    """The default Windows recording device, through WASAPI like loopback."""
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        raise CaptureUnavailable("WASAPI not available. Are you on Windows?")

    if wasapi["defaultInputDevice"] < 0:
        raise CaptureUnavailable(
            "No microphone found. Plug one in, or set a default recording "
            "device in Windows sound settings."
        )
    return p.get_device_info_by_index(wasapi["defaultInputDevice"])


def list_devices():
    """
    Every loopback device we could record from, newest default first.

    Returns a list of {"index", "name", "channels", "rate", "is_default"}.
    Returns [] rather than raising when capture is unavailable, so the UI can
    show an empty picker with an explanation instead of a stack trace.
    """
    if not AVAILABLE:
        return []

    p = pyaudio.PyAudio()
    try:
        try:
            default = _default_loopback_device(p)
            default_index = default["index"]
        except CaptureUnavailable:
            default_index = None

        devices = []
        for d in p.get_loopback_device_info_generator():
            devices.append(
                {
                    "index": d["index"],
                    "name": d["name"],
                    "channels": d["maxInputChannels"],
                    "rate": int(d["defaultSampleRate"]),
                    "is_default": d["index"] == default_index,
                }
            )
    finally:
        p.terminate()

    devices.sort(key=lambda d: (not d["is_default"], d["name"]))
    return devices


def _open_stream(p, device_index, source="mic"):
    """Resolve a device and open an input stream on it."""
    if device_index is None and source == "mic":
        device = _default_mic_device(p)
    elif device_index is None:
        device = _default_loopback_device(p)
    else:
        device = p.get_device_info_by_index(int(device_index))

    channels = device["maxInputChannels"]
    rate = int(device["defaultSampleRate"])

    stream = p.open(
        format=pyaudio.paInt16,
        channels=channels,
        rate=rate,
        frames_per_buffer=CHUNK,
        input=True,
        input_device_index=device["index"],
    )
    return stream, device, channels, rate


class Recorder:
    """
    A single background recording.

    Non-blocking: start() returns as soon as the stream is open, and the read
    loop runs on its own thread until stop() or max_minutes. One Recorder holds
    one recording at a time; starting a second while one is live raises.
    """

    def __init__(self, out_dir="recordings"):
        self.out_dir = Path(out_dir)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._state = {
            "recording": False,
            "label": None,
            "path": None,
            "device": None,
            "started_at": None,
            "elapsed": 0.0,
            "bytes": 0,
            "error": None,
        }

    # -- public API ----------------------------------------------------

    def start(self, label="meeting", device_index=None, max_minutes=180, source="mic"):
        _require_pyaudio()

        with self._lock:
            if self._state["recording"]:
                raise RuntimeError("Already recording. Stop the current one first.")

        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        safe_label = "".join(c for c in label if c.isalnum() or c in "-_") or "meeting"
        out_path = self.out_dir / f"{safe_label}_{stamp}.wav"

        # Open the stream on the calling thread so a bad device surfaces as an
        # immediate error to the caller instead of a silent dead thread.
        p = pyaudio.PyAudio()
        try:
            stream, device, channels, rate = _open_stream(p, device_index, source)
        except Exception:
            p.terminate()
            raise

        wav = wave.open(str(out_path), "wb")
        wav.setnchannels(channels)
        wav.setsampwidth(p.get_sample_size(pyaudio.paInt16))
        wav.setframerate(rate)

        self._stop.clear()
        with self._lock:
            self._state = {
                "recording": True,
                "label": safe_label,
                "path": str(out_path),
                "device": device["name"],
                "started_at": time.time(),
                "elapsed": 0.0,
                "bytes": 0,
                "error": None,
            }

        self._thread = threading.Thread(
            target=self._run,
            args=(p, stream, wav, out_path, max_minutes),
            daemon=True,
            name="recorder",
        )
        self._thread.start()
        return self.status()

    def stop(self, timeout=10):
        """Signal the loop to finish and wait for the file to be closed."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        return self.status()

    def status(self):
        with self._lock:
            state = dict(self._state)
        if state["recording"] and state["started_at"]:
            state["elapsed"] = round(time.time() - state["started_at"], 1)
        return state

    # -- internals -----------------------------------------------------

    def _run(self, p, stream, wav, out_path, max_minutes):
        start = time.time()
        deadline = start + max_minutes * 60
        written = 0
        error = None

        try:
            while not self._stop.is_set() and time.time() < deadline:
                data = stream.read(CHUNK, exception_on_overflow=False)
                wav.writeframes(data)
                written += len(data)
                with self._lock:
                    self._state["bytes"] = written
        except Exception as exc:
            error = str(exc)
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            p.terminate()
            wav.close()

            with self._lock:
                self._state["recording"] = False
                self._state["elapsed"] = round(time.time() - start, 1)
                self._state["bytes"] = written
                self._state["error"] = error


def record(out_dir="recordings", label="meeting", max_minutes=180, device_index=None, source="mic"):
    """
    Record until Ctrl+C or max_minutes elapses.

    Blocking. Returns the path to the written .wav file.
    """
    rec = Recorder(out_dir=out_dir)
    state = rec.start(label=label, device_index=device_index, max_minutes=max_minutes, source=source)

    print(f"Recording from: {state['device']}")
    print(f"Writing to:     {state['path']}")
    print("Press Ctrl+C to stop.\n")

    next_tick = 0
    try:
        while rec.status()["recording"]:
            elapsed = int(rec.status()["elapsed"])
            if elapsed >= next_tick:
                print(f"\r  {elapsed // 60:02d}:{elapsed % 60:02d} elapsed", end="", flush=True)
                next_tick = elapsed + 1
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\n\nStopped.")
        rec.stop()

    final = rec.status()
    out_path = Path(final["path"])
    if final["error"]:
        print(f"\nRecording ended with an error: {final['error']}")

    size_mb = out_path.stat().st_size / 1e6 if out_path.exists() else 0
    print(f"Saved {final['elapsed'] / 60:.1f} min, {size_mb:.0f} MB -> {out_path}")

    return out_path


if __name__ == "__main__":
    import sys

    label = sys.argv[1] if len(sys.argv) > 1 else "meeting"
    record(label=label)
