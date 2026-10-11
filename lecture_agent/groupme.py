"""
Posting an approved announcement to GroupMe through the Bot API.

Only called after the president confirms in the UI. Stdlib only.
GroupMe caps a message at 1000 characters, so long drafts go out as several
messages, split on paragraph breaks.
"""

import json
import re
import urllib.error
import urllib.request

from .config import load_bot_id

API_URL = "https://api.groupme.com/v3/bots/post"
LIMIT = 1000


class GroupMeError(RuntimeError):
    """Posting failed. The message is written for the president to read."""


class MissingBotId(GroupMeError):
    pass


def split_message(text, limit=LIMIT):
    """Pack paragraphs into as few messages as fit under the cap."""
    parts, current = [], ""
    for para in re.split(r"\n\s*\n", text.strip()):
        para = para.strip()
        if not para:
            continue
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        # One paragraph over the cap on its own: break at a line or word.
        while len(para) > limit:
            cut = max(para.rfind("\n", 0, limit + 1), para.rfind(" ", 0, limit + 1))
            if cut <= 0:
                cut = limit
            parts.append(para[:cut].rstrip())
            para = para[cut:].lstrip()
        current = para
    if current:
        parts.append(current)
    return parts


def require_bot_id():
    bot_id, _source = load_bot_id()
    if not bot_id:
        raise MissingBotId("No GroupMe bot ID yet. Open Settings and paste it in.")
    return bot_id


def post(text):
    """Send the announcement. Returns the list of messages that were posted."""
    bot_id = require_bot_id()
    parts = split_message(text)
    if not parts:
        raise GroupMeError("The announcement is empty, so nothing was posted.")

    for i, part in enumerate(parts):
        # If message 2 of 3 fails, say so: the first one is already in the chat.
        sent = f"Posted {i} of {len(parts)} messages, then: " if i else ""
        req = urllib.request.Request(
            API_URL,
            data=json.dumps({"bot_id": bot_id, "text": part}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20):
                pass
        except urllib.error.HTTPError as exc:
            hint = (
                " The bot ID looks wrong. Check it in Settings."
                if exc.code in (400, 401, 404)
                else " Try again in a minute."
            )
            raise GroupMeError(f"{sent}GroupMe refused the message (error {exc.code}).{hint}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GroupMeError(
                f"{sent}Could not reach GroupMe. Check the internet connection and try again. ({exc})"
            ) from exc
    return parts


if __name__ == "__main__":
    # Self-check: splitting, then a real round trip against a local stub.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from unittest import mock

    assert split_message("") == []
    assert split_message("hi\n\nthere") == ["hi\n\nthere"]
    a, b, c = "a" * 600, "b" * 300, "c" * 500
    assert split_message(f"{a}\n\n{b}\n\n{c}") == [f"{a}\n\n{b}", c]
    long = " ".join(["word"] * 500)  # 2499 chars, no paragraph breaks
    got = split_message(long)
    assert all(len(p) <= LIMIT for p in got) and " ".join(got) == long, got
    assert split_message("x" * 2500) == ["x" * 1000, "x" * 1000, "x" * 500]
    assert all(len(p) <= LIMIT for p in split_message(f"{a}\n\n{long}\n\n{c}"))

    received, fail_on = [], set()

    class Stub(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(body)
            self.send_response(404 if len(received) in fail_on else 202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v3/bots/post"

    with mock.patch(__name__ + ".API_URL", url), mock.patch(
        __name__ + ".load_bot_id", return_value=("BOT123", "test")
    ):
        assert post(f"{a}\n\n{b}\n\n{c}") == [f"{a}\n\n{b}", c]
        assert received == [{"bot_id": "BOT123", "text": f"{a}\n\n{b}"}, {"bot_id": "BOT123", "text": c}]

        received.clear()
        fail_on.add(2)
        try:
            post(f"{a}\n\n{b}\n\n{c}")
            raise AssertionError("expected a failure")
        except GroupMeError as exc:
            assert "Posted 1 of 2" in str(exc) and "404" in str(exc), exc

    with mock.patch(__name__ + ".load_bot_id", return_value=(None, None)):
        try:
            post("hi")
            raise AssertionError("expected MissingBotId")
        except MissingBotId:
            pass

    server.shutdown()
    print("ok")
