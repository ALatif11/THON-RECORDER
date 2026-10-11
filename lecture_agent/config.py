"""
Finding the GroupMe bot ID.

Order of preference:
  1. the GROUPME_BOT_ID environment variable
  2. groupme_bot_id.txt in the project folder (what the Settings panel writes)

The environment variable wins so a machine-wide setting cannot be silently
shadowed by a stale file. The file is gitignored: anyone holding the bot ID can
post to the group.
"""

import os
from pathlib import Path

ENV_VAR = "GROUPME_BOT_ID"
BOT_ID_FILE = "groupme_bot_id.txt"


def project_root():
    return Path(__file__).resolve().parent.parent


def _parse(text):
    """
    Pull the ID out of a file that is either a bare value or KEY=value lines.

    Tolerates comments, blank lines, quotes, and the BOM Notepad likes to add.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if "=" in line:
            name, _, value = line.partition("=")
            if name.strip().upper() != ENV_VAR:
                continue
            line = value.strip()

        line = line.strip().strip('"').strip("'").strip()
        if line:
            return line
    return None


def load_bot_id(root=None):
    """Return (bot_id, source). Source is "environment", the filename, or None."""
    env = (os.environ.get(ENV_VAR) or "").strip()
    if env:
        return env, "environment"

    path = (Path(root) if root else project_root()) / BOT_ID_FILE
    try:
        bot_id = _parse(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return None, None
    return (bot_id, BOT_ID_FILE) if bot_id else (None, None)


def save_bot_id(bot_id, root=None):
    path = (Path(root) if root else project_root()) / BOT_ID_FILE
    path.write_text(bot_id.strip() + "\n", encoding="utf-8")


def redact(value):
    """For logs and the UI. Never show the ID in full."""
    if not value:
        return "none"
    return f"...{value[-4:]}"
