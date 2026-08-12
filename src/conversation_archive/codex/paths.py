from __future__ import annotations

import os
from pathlib import Path


APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Conversation Archive Tools"
DEFAULT_INPUT_PATH = Path.home() / ".codex" / "sessions"
DEFAULT_DB_PATH = APP_SUPPORT_DIR / "codex_sessions.sqlite"


def default_input_path() -> str:
    return os.environ.get("CODEX_SESSION_INPUT", str(DEFAULT_INPUT_PATH))


def default_db_path() -> str:
    return os.environ.get("CODEX_SESSION_DB", str(DEFAULT_DB_PATH))
