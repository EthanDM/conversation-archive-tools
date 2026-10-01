"""Import one Claude export snapshot without retaining tools or reasoning."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

from .paths import default_db_path, default_input_path

SCHEMA_VERSION = "1"
SCHEMA = (
    "CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE conversations (uuid TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT, updated_at TEXT, source TEXT NOT NULL)",
    "CREATE TABLE messages (id INTEGER PRIMARY KEY, uuid TEXT UNIQUE NOT NULL, conversation_uuid TEXT NOT NULL REFERENCES conversations(uuid), parent_uuid TEXT, seq INTEGER NOT NULL, role TEXT NOT NULL, created_at TEXT, updated_at TEXT, text TEXT NOT NULL)",
    "CREATE INDEX messages_conversation_seq ON messages(conversation_uuid, seq)",
    "CREATE VIRTUAL TABLE messages_fts USING fts5(text, title, tokenize='unicode61 remove_diacritics 2')",
)


def check_database(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables:
        raise ValueError("Database is not a Claude index")
    if "index_meta" not in tables:
        raise ValueError("Refusing database without Claude provider metadata")
    metadata = dict(connection.execute("SELECT key, value FROM index_meta"))
    if metadata.get("provider") != "claude" or metadata.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Database provider or schema version is not supported for Claude")
    if not {"conversations", "messages", "messages_fts"}.issubset(tables):
        raise ValueError("Claude database is incomplete")


def open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.expanduser().resolve().as_uri() + "?mode=ro", uri=True)
    try:
        check_database(connection)
    except Exception:
        connection.close()
        raise
    connection.row_factory = sqlite3.Row
    return connection


def input_files(path: Path) -> list[Path]:
    path = path.expanduser().resolve()
    if path.is_dir():
        for candidate in (path / "extracted/conversations.json", path / "conversations.json"):
            if candidate.is_file():
                return [candidate]
        parts = sorted(path.glob("conversations-*.zip"))
        if parts:
            return parts
        raise ValueError("Export directory contains no conversations JSON or ZIP parts")
    if not path.is_file() or path.suffix.lower() not in {".json", ".zip"}:
        raise ValueError("Input must be a conversations JSON, ZIP, or export directory")
    return [path]


def load_records(path: Path) -> list[tuple[dict, str]]:
    records = []
    for file in input_files(path):
        if file.suffix.lower() == ".zip":
            with zipfile.ZipFile(file) as archive:
                members = [name for name in archive.namelist() if Path(name).name == "conversations.json"]
                if len(members) != 1:
                    raise ValueError("Each conversations ZIP must contain exactly one conversations.json")
                data = json.loads(archive.read(members[0]))
                source = f"{file}!{members[0]}"
        else:
            data = json.loads(file.read_text(encoding="utf-8"))
            source = str(file)
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("Expected a list of Claude conversations")
        records.extend((item, source) for item in data)
    return records


def optional_string(record: dict, key: str) -> str | None:
    value = record.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Expected string or null for {key}")
    return value


def required_uuid(record: dict) -> str:
    value = optional_string(record, "uuid")
    if not value or not value.strip():
        raise ValueError("Conversation and message UUIDs must be nonempty strings")
    return value


def message_text(message: dict) -> str:
    # Structured blocks are authoritative: a legacy field may include hidden payloads.
    content = message.get("content")
    if content is None:
        text = optional_string(message, "text") or ""
    else:
        if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
            raise ValueError("Message content must be a list of blocks")
        text = "\n".join(optional_string(block, "text") or "" for block in content if block.get("type") == "text")
    markers = []
    for key in ("attachments", "files"):
        items = message.get(key)
        if items is None:
            items = []
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError(f"Message {key} must be a list")
        for item in items:
            name = optional_string(item, "file_name")
            if name and name not in markers:
                markers.append(name)
    return "\n".join([text, *(f"[Attachment reference: {name}]" for name in markers)]).strip()


def parse_export(path: Path) -> tuple[list[tuple], list[tuple]]:
    conversations, messages = [], []
    conversation_ids, message_ids = set(), set()
    for record, source in load_records(path):
        uuid = required_uuid(record)
        if uuid in conversation_ids:
            raise ValueError(f"Duplicate conversation UUID: {uuid}")
        conversation_ids.add(uuid)
        title = optional_string(record, "name") or "Untitled conversation"
        conversations.append((uuid, title, optional_string(record, "created_at"), optional_string(record, "updated_at"), source))
        exported = record.get("chat_messages")
        if not isinstance(exported, list) or any(not isinstance(item, dict) for item in exported):
            raise ValueError("Conversation chat_messages must be a list")
        for seq, message in enumerate(exported, 1):
            message_uuid = required_uuid(message)
            if message_uuid in message_ids:
                raise ValueError(f"Duplicate message UUID: {message_uuid}")
            message_ids.add(message_uuid)
            sender = optional_string(message, "sender")
            if sender not in {"human", "assistant"}:
                raise ValueError(f"Unsupported Claude sender: {sender}")
            messages.append((message_uuid, uuid, optional_string(message, "parent_message_uuid"), seq, "user" if sender == "human" else sender, optional_string(message, "created_at"), optional_string(message, "updated_at"), message_text(message)))
    return conversations, messages


def index_export(input_path: Path, db_path: Path) -> tuple[int, int]:
    conversations, messages = parse_export(input_path)
    db_path = db_path.expanduser().resolve()
    sources = input_files(input_path)
    if db_path in sources:
        raise ValueError("Database path must not overwrite an input file")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    existed = db_path.exists()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        # Explicit BEGIN also makes first-time schema creation part of the transaction.
        connection.execute("BEGIN IMMEDIATE")
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if existed or tables:
            check_database(connection)
            connection.execute("DELETE FROM messages_fts")
            connection.execute("DELETE FROM messages")
            connection.execute("DELETE FROM conversations")
        else:
            for statement in SCHEMA:
                connection.execute(statement)
            connection.executemany("INSERT INTO index_meta VALUES (?, ?)", [("provider", "claude"), ("schema_version", SCHEMA_VERSION)])
        connection.executemany("INSERT INTO conversations VALUES (?, ?, ?, ?, ?)", conversations)
        connection.executemany("INSERT INTO messages(uuid, conversation_uuid, parent_uuid, seq, role, created_at, updated_at, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", messages)
        connection.execute("INSERT INTO messages_fts(rowid, text, title) SELECT m.id, m.text, c.title FROM messages m JOIN conversations c ON c.uuid=m.conversation_uuid")
    return len(conversations), len(messages)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Index a separate local Claude conversation snapshot.")
    parser.add_argument("--input", default=default_input_path())
    parser.add_argument("--db", default=default_db_path())
    args = parser.parse_args(argv)
    if not args.input:
        parser.error("provide --input or CLAUDE_EXPORT_INPUT")
    try:
        conversations, messages = index_export(Path(args.input), Path(args.db))
    except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile) as error:
        print(f"Claude import failed: {error}", file=sys.stderr)
        return 2
    print(f"Indexed {conversations} conversations and {messages} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
