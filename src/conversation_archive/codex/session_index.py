"""Filter local Codex JSONL sessions into a searchable local SQLite index.

Only meaningful user and assistant text is retained. Runtime wrappers, tool
events, and injected instructions are intentionally excluded before storage.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional


INDEXER_VERSION = "1"
TITLE_LIMIT = 140

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS index_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  source_path TEXT NOT NULL UNIQUE,
  cwd TEXT,
  title TEXT NOT NULL,
  first_time_iso TEXT,
  last_time_iso TEXT,
  file_size INTEGER NOT NULL,
  file_mtime_ns INTEGER NOT NULL,
  user_message_count INTEGER NOT NULL,
  assistant_message_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,
  create_time_iso TEXT,
  text TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
  UNIQUE(session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_last_time ON sessions(last_time_iso);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text,
  session_id UNINDEXED,
  title,
  role UNINDEXED,
  create_time_iso UNINDEXED,
  content='messages',
  content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text, session_id, title, role, create_time_iso)
  VALUES (
    new.id,
    new.text,
    new.session_id,
    (SELECT title FROM sessions WHERE session_id=new.session_id),
    new.role,
    new.create_time_iso
  );
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO messages_fts(rowid, text, session_id, title, role, create_time_iso)
  VALUES (
    new.id,
    new.text,
    new.session_id,
    (SELECT title FROM sessions WHERE session_id=new.session_id),
    new.role,
    new.create_time_iso
  );
END;
"""

WRAPPER_MARKERS = (
    "<environment_context>",
    "<app-context>",
    "<permissions instructions>",
    "<skills_instructions>",
    "<recommended_plugins>",
    "<skill>",
    "# agents.md instructions",
    "the following is the codex agent history whose request action you are assessing",
)


@dataclass(frozen=True)
class RetainedMessage:
    """One retained text message with its source role and optional event time."""
    role: str
    create_time_iso: Optional[str]
    text: str


@dataclass(frozen=True)
class ParsedSession:
    """Filtered session metadata and messages derived from one JSONL source file."""
    session_id: str
    cwd: str
    title: str
    first_time_iso: Optional[str]
    last_time_iso: Optional[str]
    messages: list[RetainedMessage]


def _normalise_timestamp(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def _clean_text(text: object) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = text.strip()
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if any(marker in lowered for marker in WRAPPER_MARKERS):
        return ""
    # IDE context is often useful only for the final request. Keep that request, not file/tab noise.
    request_marker = "## my request for codex:"
    if request_marker in lowered:
        offset = lowered.index(request_marker) + len(request_marker)
        cleaned = cleaned[offset:].strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _message_text(payload: dict[str, object], role: str) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    wanted_type = "input_text" if role == "user" else "output_text"
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != wanted_type:
            continue
        text = _clean_text(block.get("text"))
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _title_from_messages(messages: list[RetainedMessage], session_id: str) -> str:
    first_user = next((m.text for m in messages if m.role == "user"), "")
    title = re.sub(r"\s+", " ", first_user).strip() or f"Codex session {session_id}"
    return title[:TITLE_LIMIT].rstrip()


def parse_session_file(path: Path) -> Optional[ParsedSession]:
    """Parse one Codex JSONL file, skipping malformed records and excluded content.

    Returns ``None`` when the file has no stable session ID. Repeated metadata
    records remain part of the same session rather than creating duplicates.
    """
    session_id: Optional[str] = None
    cwd = ""
    messages: list[RetainedMessage] = []
    first_time: Optional[str] = None
    last_time: Optional[str] = None

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            timestamp = _normalise_timestamp(event.get("timestamp"))
            if event.get("type") == "session_meta":
                candidate_id = payload.get("id")
                if isinstance(candidate_id, str) and candidate_id:
                    session_id = session_id or candidate_id
                candidate_cwd = payload.get("cwd")
                if isinstance(candidate_cwd, str) and candidate_cwd:
                    cwd = cwd or candidate_cwd
                continue
            if event.get("type") != "response_item" or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = _message_text(payload, role)
            if not text:
                continue
            messages.append(RetainedMessage(role=role, create_time_iso=timestamp, text=text))
            first_time = first_time or timestamp
            last_time = timestamp or last_time

    if not session_id:
        return None
    return ParsedSession(
        session_id=session_id,
        cwd=cwd,
        title=_title_from_messages(messages, session_id),
        first_time_iso=first_time,
        last_time_iso=last_time,
        messages=messages,
    )


def iter_session_files(input_path: Path) -> Iterator[Path]:
    if input_path.is_file():
        yield input_path
        return
    yield from (
        path
        for path in sorted(input_path.rglob("*.jsonl"))
        if not path.is_symlink() and path.resolve().is_relative_to(input_path)
    )


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    return connection


def scope_index_to_input(cursor: sqlite3.Cursor, input_path: Path, source_paths: set[str]) -> None:
    """Remove rows outside the input or whose source file is no longer present."""
    out_of_scope = [
        (source_path,)
        for (source_path,) in cursor.execute("SELECT source_path FROM sessions")
        if (
            source_path not in source_paths
            or (
                Path(source_path) != input_path
                if input_path.is_file()
                else not Path(source_path).is_relative_to(input_path)
            )
        )
    ]
    cursor.executemany("DELETE FROM sessions WHERE source_path=?", out_of_scope)


def replace_session(
    cursor: sqlite3.Cursor, parsed: ParsedSession, source_path: str, stat: object
) -> int:
    """Replace either identity with one parsed session and return its message count."""
    cursor.execute("DELETE FROM sessions WHERE source_path=? OR session_id=?", (source_path, parsed.session_id))
    user_count = sum(message.role == "user" for message in parsed.messages)
    assistant_count = sum(message.role == "assistant" for message in parsed.messages)
    cursor.execute(
        """
        INSERT INTO sessions(
          session_id, source_path, cwd, title, first_time_iso, last_time_iso,
          file_size, file_mtime_ns, user_message_count, assistant_message_count
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            parsed.session_id,
            source_path,
            parsed.cwd,
            parsed.title,
            parsed.first_time_iso,
            parsed.last_time_iso,
            stat.st_size,
            stat.st_mtime_ns,
            user_count,
            assistant_count,
        ),
    )
    cursor.executemany(
        "INSERT INTO messages(session_id, seq, role, create_time_iso, text) VALUES (?,?,?,?,?)",
        [
            (parsed.session_id, sequence, message.role, message.create_time_iso, message.text)
            for sequence, message in enumerate(parsed.messages, start=1)
        ],
    )
    return len(parsed.messages)


def parse_session_snapshot(path: Path) -> Optional[tuple[object, ParsedSession]]:
    """Parse a stable session-file snapshot, retrying one concurrent replacement."""
    for _ in range(2):
        before = path.stat()
        parsed = parse_session_file(path)
        after = path.stat()
        if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
            return after, parsed
    return None


def index_sessions(
    input_path: Path,
    db_path: Path,
    *,
    reset: bool = False,
    incremental: bool = True,
    limit: Optional[int] = None,
) -> tuple[int, int, int]:
    """Refresh a local session index and return indexed, skipped, and retained counts.

    Incremental mode trusts the source file's size and nanosecond mtime; changed
    files replace prior rows for either their path or their session ID. ``reset``
    removes only the target SQLite database and its WAL sidecars.
    """
    input_path = input_path.expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Codex session input does not exist: {input_path}")
    if not input_path.is_file() and not input_path.is_dir():
        raise ValueError(f"Codex session input is not a file or directory: {input_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset:
        for suffix in ("", "-wal", "-shm"):
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    all_paths = list(iter_session_files(input_path))
    source_paths = {str(path.resolve()) for path in all_paths}
    paths = all_paths
    if limit is not None:
        paths = paths[:limit]
    connection = connect(db_path)
    cursor = connection.cursor()
    scope_index_to_input(cursor, input_path, source_paths)
    indexed = skipped = retained_messages = 0
    displaced_session_ids: set[str] = set()

    for path in paths:
        stat = path.stat()
        source_path = str(path.resolve())
        existing = cursor.execute(
            "SELECT session_id, file_size, file_mtime_ns FROM sessions WHERE source_path=?", (source_path,)
        ).fetchone()
        if incremental and existing:
            if existing[1] == stat.st_size and existing[2] == stat.st_mtime_ns:
                skipped += 1
                continue
        parsed = parse_session_file(path)
        if parsed is None:
            continue
        if existing and existing[0] != parsed.session_id:
            displaced_session_ids.add(existing[0])
        # Remove any prior session that occupied this path before deciding whether
        # another, newer path should retain this parsed session ID.
        cursor.execute("DELETE FROM sessions WHERE source_path=?", (source_path,))
        existing_session = cursor.execute(
            "SELECT source_path, file_mtime_ns FROM sessions WHERE session_id=?",
            (parsed.session_id,),
        ).fetchone()
        if (
            existing_session
            and existing_session[0] != source_path
            and existing_session[1] >= stat.st_mtime_ns
        ):
            skipped += 1
            continue
        retained_messages += replace_session(cursor, parsed, source_path, stat)
        indexed += 1

    if displaced_session_ids:
        displaced_candidates: dict[str, tuple[Path, object, ParsedSession]] = {}
        for path in paths:
            snapshot = parse_session_snapshot(path)
            if snapshot is None:
                continue
            stat, parsed = snapshot
            if parsed is None or parsed.session_id not in displaced_session_ids:
                continue
            candidate = displaced_candidates.get(parsed.session_id)
            if candidate is None or stat.st_mtime_ns > candidate[1].st_mtime_ns:
                displaced_candidates[parsed.session_id] = (path, stat, parsed)

        for path, stat, parsed in displaced_candidates.values():
            existing = cursor.execute(
                "SELECT session_id, file_size, file_mtime_ns FROM sessions WHERE source_path=?",
                (str(path.resolve()),),
            ).fetchone()
            if existing == (parsed.session_id, stat.st_size, stat.st_mtime_ns):
                continue
            retained_messages += replace_session(cursor, parsed, str(path.resolve()), stat)
            indexed += 1

    cursor.execute(
        "INSERT INTO index_meta(key, value) VALUES('indexer_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (INDEXER_VERSION,),
    )
    connection.commit()
    connection.close()
    return indexed, skipped, retained_messages
