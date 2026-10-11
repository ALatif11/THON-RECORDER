"""
Starts the local server and opens it in a native window.

The server binds to 127.0.0.1 on a free port and is never exposed to the
network. If pywebview is not installed (or fails to find a GUI backend) this
falls back to your default browser, which works just as well.
"""

import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TITLE = "THON Announcer"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_server(url, timeout=30, thread=None):
    """
    Poll until uvicorn is actually accepting requests.

    Bails out the moment the server thread dies, so a crashed import is
    reported in a second instead of after the full timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except urllib.error.HTTPError:
            return True  # responding, just not with a 200
        except Exception:
            if thread is not None and not thread.is_alive():
                return False
            time.sleep(0.15)
    return False


REQUIRED = [
    ("uvicorn", "uvicorn"),
    ("fastapi", "fastapi"),
]

# Set by the server thread if it dies, so launch() can report the real cause
# instead of a bare "did not start in time".
_server_error = []


def check_dependencies():
    """Fail early and legibly rather than deep inside a background thread."""
    missing = [pkg for mod, pkg in REQUIRED if not _importable(mod)]
    if not missing:
        return

    exe = Path(sys.executable).name
    sys.exit(
        "Missing required packages: "
        + ", ".join(missing)
        + "\n\nInstall them with:\n"
        + f"    {exe} -m pip install -r requirements.txt\n\n"
        + "Run it from this folder, and make sure it is the same Python you "
        + f"are launching with ({sys.executable})."
    )


def _importable(module):
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def serve(port):
    try:
        import uvicorn

        from app.server import app

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except BaseException as exc:
        _server_error.append(f"{type(exc).__name__}: {exc}")
        raise


def launch(port=0, browser=False):
    check_dependencies()

    port = port or free_port()
    url = f"http://127.0.0.1:{port}"

    thread = threading.Thread(target=serve, args=(port,), daemon=True, name="uvicorn")
    thread.start()

    if not wait_for_server(url, thread=thread):
        if _server_error:
            sys.exit(f"The server failed to start.\n  {_server_error[0]}")
        sys.exit("The server did not start in time. Try running it again.")

    print(f"THON Announcer running at {url}")

    if browser:
        webbrowser.open(url)
        _idle(thread)
        return

    try:
        import webview
    except ImportError:
        print("pywebview is not installed, opening in your browser instead.")
        print("For a real app window: pip install pywebview")
        webbrowser.open(url)
        _idle(thread)
        return

    try:
        webview.create_window(TITLE, url, width=1280, height=860, min_size=(900, 600))
        webview.start()
    except Exception as exc:
        print(f"Could not open an app window ({exc}). Falling back to your browser.")
        webbrowser.open(url)
        _idle(thread)


def _idle(thread):
    """Keep the process (and the daemon server thread) alive."""
    print("Close this window or press Ctrl+C to quit.")
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    launch(browser="--browser" in sys.argv)
