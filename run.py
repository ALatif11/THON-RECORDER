"""
THON Announcer CLI.

    python run.py ui                      # open the desktop app
    python run.py record meeting          # record the mic until Ctrl+C
    python run.py process recordings/meeting_2026-10-11_1900.wav
    python run.py watch                   # process any new .wav dropped in recordings/
    python run.py devices                 # list capturable audio devices

Typical use: run `ui` and drive everything from there. The CLI is still here
for when you want to script it, or process a backlog over lunch.
"""

import argparse
import sys
import time
from pathlib import Path

from lecture_agent.library import Library


def cmd_process(args):
    from lecture_agent import pipeline

    try:
        result = pipeline.process(args.audio, model_size=args.model, title=args.title)
    except FileNotFoundError as e:
        sys.exit(str(e))

    print(f"\nNotes:      {result['markdown_path']}")
    print(f"Structured: {result['json_path']}")


def cmd_watch(args):
    from lecture_agent import pipeline

    lib = Library()
    lib.recordings_dir.mkdir(parents=True, exist_ok=True)
    lib.notes_dir.mkdir(parents=True, exist_ok=True)

    seen = {item["stem"] for item in lib.lectures() if item["has_notes"]}
    print(f"Watching {lib.recordings_dir}/ for new recordings. Ctrl+C to stop.")

    try:
        while True:
            for wav in sorted(lib.recordings_dir.glob("*.wav")):
                if wav.stem in seen:
                    continue
                # Wait for the file to stop growing before touching it
                size = wav.stat().st_size
                time.sleep(3)
                if wav.stat().st_size != size:
                    continue
                print(f"\nNew recording: {wav.name}")
                try:
                    pipeline.process(wav, model_size=args.model)
                except Exception as e:
                    print(f"Failed: {e}")
                seen.add(wav.stem)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nStopped watching.")


def cmd_record(args):
    from lecture_agent import capture

    try:
        capture.record(
            label=args.label, max_minutes=args.max_minutes, device_index=args.device, source=args.source
        )
    except capture.CaptureUnavailable as e:
        sys.exit(str(e))


def cmd_devices(args):
    from lecture_agent import capture

    devices = capture.list_devices()
    if not devices:
        sys.exit(
            "No loopback devices found. System audio capture needs Windows "
            "with PyAudioWPatch installed."
        )
    for d in devices:
        marker = "*" if d["is_default"] else " "
        print(f" {marker} [{d['index']:>3}] {d['name']}  ({d['channels']}ch @ {d['rate']}Hz)")
    print("\n* = current default output")


def cmd_doctor(args):
    import platform

    from lecture_agent import capture, transcribe

    print(f"Python        {platform.python_version()}  ({sys.executable})")
    print(f"Platform      {platform.system()} {platform.release()}")
    print()

    print(f"Audio capture {'yes' if capture.AVAILABLE else 'NO'}")
    if not capture.AVAILABLE:
        print(f"              {capture.IMPORT_ERROR}")
        print("              pip install PyAudioWPatch   (Windows only)")

    info = transcribe.diagnostics()
    print(f"faster-whisper {'yes' if info['faster_whisper'] else 'NO'}")
    if not info["faster_whisper"]:
        print(f"              {info['faster_whisper_error']}")
    print(f"CTranslate2   {info['ctranslate2_version'] or 'not installed'}")
    print()

    print(f"CUDA GPUs     {info['cuda_device_count']}")

    libs = info.get("cuda_libraries") or {}
    if info.get("cuda_lib_dir"):
        print(f"  using bundled libraries in {info['cuda_lib_dir']}")
    if libs:
        for name, ok in libs.items():
            print(f"  {'found  ' if ok else 'MISSING'} {name}")

    if info["cuda_device_count"] and not info.get("cuda_usable"):
        # The GPU is visible to the driver but the maths libraries are absent,
        # which is the one case that looks fine until a model is actually loaded.
        print("Will run on   cpu  (GPU detected, but its libraries cannot be loaded)")
    else:
        print(f"Will run on   {info['device']}  (compute type {info['compute_type']})")

    if info["supported_cuda"]:
        print(f"  GPU types   {', '.join(info['supported_cuda'])}")
    if info["supported_cpu"]:
        print(f"  CPU types   {', '.join(info['supported_cpu'])}")
    print()

    if info["cuda_device_count"] == 0:
        print("No NVIDIA GPU visible to CTranslate2, so transcription runs on CPU.")
        print("Non-NVIDIA GPUs are not supported by CTranslate2 at all.")
        print()
    elif not info.get("cuda_usable"):
        missing = [n for n, ok in libs.items() if not ok]
        print(f"Your GPU is visible, but these are missing: {', '.join(missing)}")
        print()
        print(transcribe.cuda_hint())
        print()
        print("Until then transcription still works, just on the CPU.")
        print()

    from lecture_agent import config, summarize

    try:
        summarize._ollama("Reply with OK.")
        print(f"Ollama        OK  ({summarize.MODEL} at {summarize.HOST})")
    except Exception as e:
        print(f"Ollama        NOT WORKING: {e}")

    bot_id, source = config.load_bot_id()
    if bot_id:
        print(f"GroupMe bot   {config.redact(bot_id)}  (from {source})")
    else:
        print("GroupMe bot   NOT SET  (paste it in Settings, or set GROUPME_BOT_ID)")


def cmd_ui(args):
    from app.desktop import launch

    launch(port=args.port, browser=args.browser)


def main():
    ap = argparse.ArgumentParser(description="Record meetings and draft announcements.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="record the microphone or system audio")
    rec.add_argument("label", nargs="?", default="meeting")
    rec.add_argument("--source", choices=("mic", "speakers"), default="mic")
    rec.add_argument("--max-minutes", type=int, default=180)
    rec.add_argument("--device", type=int, default=None, help="loopback device index, see `devices`")
    rec.set_defaults(func=cmd_record)

    proc = sub.add_parser("process", help="transcribe and summarize an audio file")
    proc.add_argument("audio")
    proc.add_argument("--model", default="auto")
    proc.add_argument("--title")
    proc.set_defaults(func=cmd_process)

    w = sub.add_parser("watch", help="auto-process new recordings")
    w.add_argument("--model", default="auto")
    w.add_argument("--poll", type=int, default=10)
    w.set_defaults(func=cmd_watch)

    d = sub.add_parser("devices", help="list capturable audio devices")
    d.set_defaults(func=cmd_devices)

    doc = sub.add_parser("doctor", help="report what is installed and whether the GPU is usable")
    doc.set_defaults(func=cmd_doctor)

    u = sub.add_parser("ui", help="open the desktop app")
    u.add_argument("--port", type=int, default=0, help="0 picks a free port")
    u.add_argument("--browser", action="store_true", help="use your browser, not a window")
    u.set_defaults(func=cmd_ui)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    main()
