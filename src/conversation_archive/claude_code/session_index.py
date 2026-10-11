"""Index Claude Code text without storing thinking, tools, or injected events.

File order is retained; parent UUIDs and sidechain flags describe branches rather
than selecting one. Malformed JSONL lines are tolerated for active-session tails.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..codex.session_index import SCHEMA_SQL

VERSION = "2"

CLAUDE_FTS_TRIGGERS = (
    """CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text, session_id, title, role, create_time_iso)
  VALUES (new.id, new.text, new.session_id, new.title, new.role, new.create_time_iso);
END""",
    """CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text, session_id, title, role, create_time_iso)
  VALUES ('delete', old.id, old.text, old.session_id, old.title, old.role, old.create_time_iso);
END""",
    """CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text, session_id, title, role, create_time_iso)
  VALUES ('delete', old.id, old.text, old.session_id, old.title, old.role, old.create_time_iso);
  INSERT INTO messages_fts(rowid, text, session_id, title, role, create_time_iso)
  VALUES (new.id, new.text, new.session_id, new.title, new.role, new.create_time_iso);
END""",
)


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


def _replace_fts_triggers(connection: sqlite3.Connection) -> None:
    for name in ("messages_ai", "messages_ad", "messages_au"):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    for trigger in CLAUDE_FTS_TRIGGERS:
        connection.execute(trigger)


def _migrate_database(connection: sqlite3.Connection) -> None:
    """Upgrade the first Claude Code schema and rebuild its incomplete FTS index."""
    metadata = dict(connection.execute("SELECT key, value FROM index_meta"))
    if metadata.get("provider") != "claude_code" or metadata.get("schema_version") != "1":
        check_database(connection)
        return
    with connection:
        connection.execute("ALTER TABLE messages ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        connection.execute(
            "UPDATE messages SET title=(SELECT title FROM sessions WHERE sessions.session_id=messages.session_id)"
        )
        _replace_fts_triggers(connection)
        connection.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        connection.execute("UPDATE index_meta SET value=? WHERE key='schema_version'", (VERSION,))


def _session_stream_id(path: Path, observed_ids: list[str]) -> str:
    """Return the stable transcript-stream identity represented by one source file.

    Fork files may start with copied ancestor events, so a matching filename or
    the latest observed ID owns the stream. Subagent files share their parent's
    event ID and therefore add their path below ``subagents`` as a discriminator.
    """
    owning_id = path.stem if path.stem in observed_ids else observed_ids[-1]
    for parent in path.parents:
        if parent.name == "subagents":
            relative_path = path.relative_to(parent).with_suffix("")
            return f"{owning_id}:subagent:{':'.join(relative_path.parts)}"
    return owning_id


def parse_session_file(path: Path) -> dict | None:
    """Retain text blocks and source identifiers, deduplicating repeated UUIDs.

    Tool-result user records and most metadata messages, compaction summaries,
    system-origin prompts, or task notifications do not represent conversation
    text. External channel messages are genuine human prompts even though Claude
    Code marks them as metadata. An identified, fully parsed session may contain
    no retained messages; None means the file lacked enough valid data to update safely.
    """
    session_ids, cwd, title = [], "", ""
    saw_message_event = False
    has_malformed_line = False
    messages, seen = [], set()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                has_malformed_line = True
                continue
            if not isinstance(event, dict):
                continue
            candidate = event.get("sessionId")
            if isinstance(candidate, str) and candidate:
                if candidate not in session_ids:
                    session_ids.append(candidate)
            if isinstance(event.get("cwd"), str):
                cwd = event["cwd"] or cwd
            if event.get("type") == "custom-title" and isinstance(event.get("customTitle"), str):
                title = event["customTitle"]
            role = event.get("type")
            message = event.get("message")
            if role in {"user", "assistant"} and isinstance(message, dict):
                saw_message_event = True
            origin = event.get("origin")
            is_external_channel_message = (role == "user" and event.get("isMeta")
                                           and isinstance(origin, dict)
                                           and origin.get("kind") == "channel"
                                           and event.get("userType") == "external")
            if (role not in {"user", "assistant"}
                    or (event.get("isMeta") and not is_external_channel_message)
                    or event.get("isCompactSummary") or event.get("promptSource") == "system"
                    or (isinstance(origin, dict) and origin.get("kind") == "task-notification")):
                continue
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
    if not session_ids or (not messages and (not saw_message_event or has_malformed_line)):
        return None
    session_id = _session_stream_id(path, session_ids)
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
            _migrate_database(connection)
        else:
            # Use the Codex retrieval schema, with Claude-specific provenance columns.
            connection.executescript(SCHEMA_SQL)
            connection.execute("ALTER TABLE messages ADD COLUMN uuid TEXT")
            connection.execute("ALTER TABLE messages ADD COLUMN parent_uuid TEXT")
            connection.execute("ALTER TABLE messages ADD COLUMN is_sidechain INTEGER NOT NULL DEFAULT 0")
            connection.execute("ALTER TABLE messages ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            _replace_fts_triggers(connection)
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
            if not messages:
                with connection:
                    connection.execute("DELETE FROM sessions WHERE source_path=?", (str(path),))
                continue
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
                connection.executemany("INSERT INTO messages (session_id, seq, role, create_time_iso, text, uuid, parent_uuid, is_sidechain, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                       [(session_id, seq, *message, session["title"])
                                        for seq, message in enumerate(messages, 1)])
            indexed += 1
        total = connection.execute("SELECT count(*) FROM messages").fetchone()[0]
        return indexed, skipped, total
    finally:
        connection.close()
