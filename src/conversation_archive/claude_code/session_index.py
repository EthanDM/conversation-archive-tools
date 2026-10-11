"""Index Claude Code text without storing thinking, tools, or injected events.

File order is retained; parent UUIDs and sidechain flags describe branches rather
than selecting one. Malformed JSONL lines are tolerated for active-session tails.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..codex.session_index import SCHEMA_SQL

VERSION = "1"


def check_database(connection: sqlite3.Connection) -> None:
    """Reject other providers and incompatible schemas before reading or writing."""
    metadata = dict(connection.execute("SELECT key, value FROM index_meta"))
    if metadata.get("provider") != "claude_code" or metadata.get("schema_version") != VERSION:
        raise ValueError("Database is not a supported Claude Code session index")


def connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        check_database(connection)
    except Exception:
        connection.close()
        raise
    connection.row_factory = sqlite3.Row
    return connection


def parse_session_file(path: Path) -> dict | None:
    """Retain text blocks and source identifiers, deduplicating repeated UUIDs.

    A file must describe one session. Tool-result user records and messages marked
    as metadata, compaction summaries, or system-origin prompts do not represent
    conversation text.
    """
    session_id, cwd, title = None, "", ""
    messages, seen = [], set()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            candidate = event.get("sessionId")
            if isinstance(candidate, str) and candidate:
                if session_id and candidate != session_id:
                    raise ValueError(f"Multiple session IDs in {path}")
                session_id = candidate
            if isinstance(event.get("cwd"), str):
                cwd = event["cwd"] or cwd
            if event.get("type") == "custom-title" and isinstance(event.get("customTitle"), str):
                title = event["customTitle"]
            role = event.get("type")
            if (role not in {"user", "assistant"} or event.get("isMeta")
                    or event.get("isCompactSummary") or event.get("promptSource") == "system"):
                continue
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                text = "\n\n".join(block["text"] for block in content
                                     if isinstance(block, dict) and block.get("type") == "text"
                                     and isinstance(block.get("text"), str)).strip()
            else:
                continue
            uuid = event.get("uuid")
            if not text or not isinstance(uuid, str) or not uuid or uuid in seen:
                continue
            seen.add(uuid)
            timestamp = event.get("timestamp")
            parent = event.get("parentUuid")
            messages.append((role, timestamp if isinstance(timestamp, str) else None, text,
                             uuid, parent if isinstance(parent, str) else None, bool(event.get("isSidechain"))))
    if not session_id or not messages:
        return None
    title = title or next((m[2] for m in messages if m[0] == "user"), f"Claude Code session {session_id}")
    return {"session_id": session_id, "cwd": cwd, "title": " ".join(title.split())[:140], "messages": messages}


def index_sessions(input_path: Path, db_path: Path, *, incremental: bool = True) -> tuple[int, int, int]:
    """Replace changed sessions atomically; retain indexed history if sources vanish.

    Returns indexed-file, unchanged-file, and total retained-message counts. Each
    file is replaced in one transaction; a failure preserves its previous index.
    """
    input_path, db_path = input_path.expanduser().resolve(), db_path.expanduser().resolve()
    if not input_path.exists():
        raise ValueError(f"Session input does not exist: {input_path}")
    files = [input_path] if input_path.is_file() else sorted(input_path.rglob("*.jsonl"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if tables:
            check_database(connection)
        else:
            # Use the Codex retrieval schema, with Claude-specific provenance columns.
            connection.executescript(SCHEMA_SQL)
            connection.execute("ALTER TABLE messages ADD COLUMN uuid TEXT")
            connection.execute("ALTER TABLE messages ADD COLUMN parent_uuid TEXT")
            connection.execute("ALTER TABLE messages ADD COLUMN is_sidechain INTEGER NOT NULL DEFAULT 0")
            connection.executemany("INSERT INTO index_meta VALUES (?, ?)", [("provider", "claude_code"), ("schema_version", VERSION)])
            connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        indexed = skipped = 0
        for path in files:
            if path.is_symlink() or (input_path.is_dir() and not path.resolve().is_relative_to(input_path)):
                continue
            stat = path.stat()
            previous = connection.execute("SELECT file_size, file_mtime_ns FROM sessions WHERE source_path=?", (str(path),)).fetchone()
            if incremental and previous == (stat.st_size, stat.st_mtime_ns):
                skipped += 1
                continue
            session = parse_session_file(path)
            if session is None:
                continue
            session_id, messages = session["session_id"], session["messages"]
            owner = connection.execute("SELECT source_path FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if owner and owner[0] != str(path):
                raise ValueError(f"Duplicate session ID in different source files: {session_id}")
            times = [m[1] for m in messages if m[1]]
            with connection:
                connection.execute("DELETE FROM sessions WHERE source_path=?", (str(path),))
                connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   (session_id, str(path), session["cwd"], session["title"], min(times) if times else None,
                                    max(times) if times else None, stat.st_size, stat.st_mtime_ns,
                                    sum(m[0] == "user" for m in messages), sum(m[0] == "assistant" for m in messages)))
                connection.executemany("INSERT INTO messages (session_id, seq, role, create_time_iso, text, uuid, parent_uuid, is_sidechain) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                       [(session_id, seq, *message) for seq, message in enumerate(messages, 1)])
            indexed += 1
        total = connection.execute("SELECT count(*) FROM messages").fetchone()[0]
        return indexed, skipped, total
    finally:
        connection.close()
