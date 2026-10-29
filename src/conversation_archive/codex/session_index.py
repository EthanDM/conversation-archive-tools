"""Filter local Codex JSONL sessions into a searchable local SQLite index.

Only meaningful user and assistant text is retained. Runtime wrappers, tool
events, and injected instructions are intentionally excluded before storage.
"""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .session_files import is_session_file, session_lines


INDEXER_VERSION = "2"
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
  DELETE FROM messages_fts WHERE rowid=old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  DELETE FROM messages_fts WHERE rowid=old.id;
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
    "<external_codex_apps_open_page>",
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



def is_approval_envelope(text: object) -> bool:
    """Recognize leading machine approval envelopes, not mentions within a human request."""
    return isinstance(text, str) and text.strip().lower().startswith((
        ">>> retained user instructions start", ">>> transcript start",
        "the following is the codex agent history added since your last approval assessment",
        "the following is the codex agent history whose request action you are assessing",
    ))

def _clean_text(text: object) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = text.strip()
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if is_approval_envelope(cleaned):
        return ""
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

    Excluded sessions and plain files without an ID return ``None``. Compressed
    files without an ID raise ValueError to protect previously indexed history.
    Repeated metadata records remain part of the same session.
    """
    session_id: Optional[str] = None
    cwd = ""
    messages: list[RetainedMessage] = []
    first_time: Optional[str] = None
    last_time: Optional[str] = None
    excluded = False

    for line in session_lines(path):
        if excluded:
            continue
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
            source = payload.get("source")
            guardian = (isinstance(source, dict)
                        and isinstance(source.get("subagent"), dict)
                        and source["subagent"].get("other") == "guardian")
            if payload.get("thread_source") == "guardian_review" or guardian:
                excluded = True
                continue
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
        if role == "user" and any(
            is_approval_envelope(block.get("text"))
            for block in payload.get("content", []) if isinstance(block, dict)
        ):
            excluded = True
            continue
        text = _message_text(payload, role)
        if not text:
            continue
        messages.append(RetainedMessage(role=role, create_time_iso=timestamp, text=text))
        first_time = first_time or timestamp
        last_time = timestamp or last_time

    if excluded:
        return None
    if not session_id:
        if path.name.endswith(".jsonl.zst"):
            raise ValueError(f"Compressed Codex session has no session ID: {path}")
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
        for path in sorted(input_path.rglob("*.jsonl*"))
        if is_session_file(path) and not path.is_symlink() and path.resolve().is_relative_to(input_path)
    )


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA_SQL)
        repair_session_fts(connection)
        return connection
    except BaseException:
        connection.close()
        raise



def repair_session_fts(connection: sqlite3.Connection) -> None:
    """Replace the legacy external-content FTS table atomically, preserving message IDs.

    Session titles are joined from sessions, not columns in messages. A standalone
    FTS table stores that projection so bm25/snippet and deletions work reliably.
    Both Codex and Claude Code share this storage schema.
    """
    row = connection.execute("SELECT sql FROM sqlite_master WHERE name='messages_fts'").fetchone()
    if row is None or not re.search(r"content\s*=", row[0], re.I):
        return
    # DDL via execute stays inside the transaction; executescript would commit early.
    with connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        for name in ("messages_ai", "messages_ad", "messages_au"):
            connection.execute(f"DROP TRIGGER IF EXISTS {name}")
        connection.execute("DROP TABLE messages_fts")
        connection.execute("""CREATE VIRTUAL TABLE messages_fts USING fts5(
            text, session_id UNINDEXED, title, role UNINDEXED, create_time_iso UNINDEXED,
            tokenize='unicode61 remove_diacritics 2')""")
        connection.execute("""INSERT INTO messages_fts(rowid,text,session_id,title,role,create_time_iso)
            SELECT m.id,m.text,m.session_id,s.title,m.role,m.create_time_iso
            FROM messages m JOIN sessions s USING(session_id)""")
        connection.execute("""CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid,text,session_id,title,role,create_time_iso)
            VALUES(new.id,new.text,new.session_id,(SELECT title FROM sessions WHERE session_id=new.session_id),new.role,new.create_time_iso); END""")
        connection.execute("""CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid=old.id; END""")
        connection.execute("""CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid=old.id;
            INSERT INTO messages_fts(rowid,text,session_id,title,role,create_time_iso)
            VALUES(new.id,new.text,new.session_id,(SELECT title FROM sessions WHERE session_id=new.session_id),new.role,new.create_time_iso); END""")

def scope_index_to_input(
    cursor: sqlite3.Cursor,
    input_path: Path,
    source_paths: set[str],
    *,
    remove_missing: bool,
    additional_inputs: tuple[Path, ...] = (),
) -> None:
    """Remove rows outside all explicit inputs or whose source file is no longer present."""
    roots = (input_path, *additional_inputs)
    out_of_scope = [
        (source_path,)
        for (source_path,) in cursor.execute("SELECT source_path FROM sessions")
        if (
            (remove_missing and source_path not in source_paths)
            or not any(
                Path(source_path) == root or (root.is_dir() and root in Path(source_path).parents)
                for root in roots
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
    additional_inputs: tuple[Path, ...] = (),
) -> tuple[int, int, int]:
    """Refresh a local session index and return indexed, skipped, and retained counts.

    Incremental mode trusts the source file's size and nanosecond mtime; changed
    files replace prior rows for either their path or their session ID. ``reset``
    builds a separate index and preserves the existing file if rebuilding fails.
    Callers must prevent raw file restores or copies at the index path throughout
    reset; SQLite locks cannot coordinate with operations that bypass SQLite.
    Explicit additional inputs form one source union; all roots must exist before writes.
    """
    input_path = input_path.expanduser().resolve()
    additional_inputs = tuple(path.expanduser().resolve() for path in additional_inputs)
    for root in additional_inputs:
        if not root.exists():
            raise FileNotFoundError("Additional Codex session input does not exist")
        if not root.is_file() and not root.is_dir():
            raise ValueError("Additional Codex input must be a file or directory")
    if not input_path.exists():
        raise FileNotFoundError(f"Codex session input does not exist: {input_path}")
    if not input_path.is_file() and not input_path.is_dir():
        raise ValueError(f"Codex session input is not a file or directory: {input_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    all_paths = sorted({path for root in (input_path, *additional_inputs) for path in iter_session_files(root)})
    source_paths = {str(path.resolve()) for path in all_paths}
    paths = all_paths
    if limit is not None:
        paths = paths[:limit]
    if reset:
        return _reset_index(db_path, input_path, additional_inputs, source_paths, paths, limit)
    connection = connect(db_path)
    try:
        return _refresh_index(connection, input_path, additional_inputs, source_paths, paths, limit, incremental, False)
    finally:
        connection.close()


def _reset_index(
    db_path: Path, input_path: Path, additional_inputs: tuple[Path, ...],
    source_paths: set[str], paths: list[Path], limit: Optional[int],
) -> tuple[int, int, int]:
    """Rebuild separately and replace the index only while other SQLite writers are excluded."""
    original = db_path.stat() if db_path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{db_path.name}.", suffix=".tmp", dir=db_path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        connection = connect(temporary)
        try:
            result = _refresh_index(connection, input_path, additional_inputs, source_paths, paths, limit, False, False)
        finally:
            connection.close()
        if original is None:
            _install_missing_index(db_path, temporary)
            return result
        _replace_index_under_lock(db_path, temporary, original)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def _install_missing_index(db_path: Path, temporary: Path) -> None:
    """Install under a SQLite lock, rejecting sidecars that appear during linking."""
    connection = sqlite3.connect(temporary, timeout=0)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        _check_index_sidecars(db_path)
        # Linking fails if another process creates the main database meanwhile.
        os.link(temporary, db_path)
        try:
            _check_index_sidecars(db_path)
        except BaseException:
            if db_path.exists() and os.path.samefile(db_path, temporary):
                db_path.unlink()
            raise
    finally:
        connection.close()


def _check_index_sidecars(db_path: Path) -> None:
    """Refuse to install a new index while SQLite recovery files remain."""
    for suffix in ("-wal", "-shm", "-journal"):
        if db_path.with_name(db_path.name + suffix).exists():
            raise ValueError(f"Cannot safely reset an index with SQLite sidecars: {db_path}")


def _replace_index_under_lock(db_path: Path, temporary: Path, original: os.stat_result) -> None:
    """Hold SQLite's exclusive lock across validation and replacement of an existing index."""
    lock_connection = None
    lock_fd = None
    _check_index_sidecars(db_path)
    try:
        try:
            lock_connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0)
            lock_connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.DatabaseError as error:
            if lock_connection is not None:
                lock_connection.close()
                lock_connection = None
            if not any(message in str(error).lower() for message in (
                "file is not a database", "database disk image is malformed", "malformed database schema",
            )):
                raise
            # SQLite cannot open a corrupt file, so acquire its POSIX lock bytes directly.
            lock_fd = os.open(db_path, os.O_RDWR)
            try:
                fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 512, 0x40000000, os.SEEK_SET)
            except OSError as lock_error:
                if lock_error.errno in (errno.EACCES, errno.EAGAIN):
                    raise ValueError(f"Cannot reset an index while it is in use: {db_path}") from lock_error
                raise
        _check_index_sidecars(db_path)
        current = db_path.stat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns
        ):
            raise ValueError(f"Cannot reset an index that changed during recovery: {db_path}")
        os.replace(temporary, db_path)
    finally:
        if lock_connection is not None:
            lock_connection.close()
        if lock_fd is not None:
            os.close(lock_fd)


def _refresh_index(
    connection: sqlite3.Connection, input_path: Path, additional_inputs: tuple[Path, ...],
    source_paths: set[str], paths: list[Path], limit: Optional[int], incremental: bool, reset: bool,
) -> tuple[int, int, int]:
    """Update one index transaction and return indexed, skipped, and message counts."""
    # All removals and replacements share one transaction: an unreadable compressed
    # replacement must not erase the prior searchable session.
    cursor = connection.cursor()
    prior_paths = {row[0] for row in cursor.execute("SELECT source_path FROM sessions")}
    if reset:
        cursor.execute("DELETE FROM sessions")
        cursor.execute("DELETE FROM index_meta")
        incremental = False
    version = cursor.execute("SELECT value FROM index_meta WHERE key='indexer_version'").fetchone()
    if version != (INDEXER_VERSION,):
        if limit is not None and cursor.execute("SELECT count(*) FROM sessions").fetchone()[0]:
            raise ValueError("Filter-version migration requires a full refresh without limit")
        incremental = False
    scope_index_to_input(cursor, input_path, source_paths, remove_missing=limit is None,
                         additional_inputs=additional_inputs)
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
            original_path = source_path[:-4] if source_path.endswith(".jsonl.zst") else source_path + ".zst"
            if original_path in prior_paths and original_path not in source_paths:
                raise ValueError(f"Cannot recover indexed session from replacement: {path}")
            if existing:
                displaced_session_ids.add(existing[0])
                cursor.execute("DELETE FROM sessions WHERE source_path=?", (source_path,))
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
    cursor.execute("INSERT INTO index_meta(key,value) VALUES('source_roots',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (json.dumps([str(root) for root in (input_path, *additional_inputs)]),))
    cursor.execute("INSERT INTO index_meta(key,value) VALUES('source_root',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(input_path),))
    cursor.execute("INSERT INTO index_meta(key,value) VALUES('last_refresh_completed_at',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (dt.datetime.now(dt.timezone.utc).isoformat(),))
    connection.commit()
    return indexed, skipped, retained_messages
