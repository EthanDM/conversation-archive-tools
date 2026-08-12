from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path
from typing import Optional, Set


def connect_sqlite(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def parse_roles(value: Optional[str]) -> Optional[Set[str]]:
    if value is None:
        return None
    return {role.strip().lower() for role in value.split(",") if role.strip()}


def truncate_text(text: str, max_chars: Optional[int]) -> str:
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def format_snippet(text: str, *, width: int, max_lines: int) -> str:
    wrapped = textwrap.wrap(text, width=width, replace_whitespace=False, drop_whitespace=False)
    if len(wrapped) > max_lines:
        wrapped = wrapped[:max_lines] + ["…"]
    return "\n".join(wrapped)
