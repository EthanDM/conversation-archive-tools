from __future__ import annotations

import os
import re
from pathlib import Path

from ..chatgpt.paths import configured_archive_root


APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Conversation Archive Tools"
DEFAULT_INPUT_PATH = Path.home() / ".codex" / "sessions"
DEFAULT_DB_PATH = APP_SUPPORT_DIR / "codex_sessions.sqlite"
MACHINE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def default_input_path() -> str:
    return os.environ.get("CODEX_SESSION_INPUT", str(DEFAULT_INPUT_PATH))


def default_db_path() -> str:
    return os.environ.get("CODEX_SESSION_DB", str(DEFAULT_DB_PATH))


def default_shared_input_path() -> str:
    configured = os.environ.get("CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT")
    if configured:
        return configured
    return str(configured_archive_root() / "codex-sessions")


def default_machine_id() -> str | None:
    value = os.environ.get("CONVERSATION_ARCHIVE_MACHINE_ID")
    return value.strip() if value else None


def validate_machine_id(value: str) -> str:
    machine_id = value.strip()
    if not MACHINE_ID_PATTERN.fullmatch(machine_id):
        raise ValueError(
            "Machine ID must be a lowercase slug using letters, numbers, underscores, or hyphens."
        )
    return machine_id
