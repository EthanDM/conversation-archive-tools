"""Codex and Claude Code sessions that must never be indexed or returned.

Every ``*.txt`` file in the exclusions folder lists session IDs, one per line; blank
lines and ``#`` comments are ignored. Several files let separate owners keep their own
list: an app that hides some of its sessions from agents can write one file and rewrite
it whenever that set changes.

A session is excluded when its own ID is listed, or, for a Claude Code subagent stream
(``<id>:subagent:…``), when its owning ID is. While indexing, a Codex session is also
excluded when its parent thread or fork source is listed, since it carries that
session's text. A malformed or unreadable list raises rather than answering without
the filter.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .codex.paths import APP_SUPPORT_DIR

DEFAULT_EXCLUSIONS_DIR = APP_SUPPORT_DIR / "excluded-sessions"
# Codex and Claude Code session IDs are UUIDs. The pattern also makes inlining the IDs
# into SQL safe (see ``sql_condition``).
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def exclusions_dir() -> Path:
    return Path(os.environ.get("CONVERSATION_ARCHIVE_EXCLUDED_SESSIONS_DIR", str(DEFAULT_EXCLUSIONS_DIR)))


def excluded_session_ids(directory: Path | None = None) -> frozenset[str]:
    """Read every list in the folder; a missing folder excludes nothing."""
    directory = (directory or exclusions_dir()).expanduser()
    if not directory.exists():
        return frozenset()
    ids = set()
    for path in sorted(directory.glob("*.txt")):
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            if not SESSION_ID_PATTERN.fullmatch(value):
                raise ValueError(f"Invalid session ID in exclusion list {path.name}")
            ids.add(value)
    return frozenset(ids)


def owning_id(session_id: str) -> str:
    """The session a Claude Code subagent stream belongs to; any other ID is its own."""
    return session_id.split(":", 1)[0]


def is_excluded(session_id: str, excluded: frozenset[str]) -> bool:
    return owning_id(session_id) in excluded


def sql_condition(column: str, excluded: frozenset[str]) -> str:
    """A SQL predicate true for rows whose session is not excluded.

    The IDs are inlined because the read-only connections cannot hold a temporary
    table; ``SESSION_ID_PATTERN`` admits no quote or other SQL syntax.
    """
    if not excluded:
        return "1=1"
    listed = ",".join(f"'{value}'" for value in sorted(excluded))
    owner = f"CASE WHEN instr({column},':')>0 THEN substr({column},1,instr({column},':')-1) ELSE {column} END"
    return f"({owner}) NOT IN ({listed})"


def exclusions_digest(excluded: frozenset[str]) -> str:
    """Stored with an index so a changed list forces every source file to be re-read."""
    return hashlib.sha256("\n".join(sorted(excluded)).encode()).hexdigest()
