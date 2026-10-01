from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / "Library/Application Support/Conversation Archive Tools/claude.sqlite"


def default_db_path() -> str:
    return os.environ.get("CLAUDE_EXPORT_DB", str(DEFAULT_DB_PATH))


def default_input_path() -> str | None:
    return os.environ.get("CLAUDE_EXPORT_INPUT")
