from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .paths import (
    configured_archive_root,
    default_db_path,
    default_historical_db_path,
)
from .index import (
    _extract_message_text,
    _iter_json_array,
    _iter_messages_all_nodes,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  content_hash TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  source_path TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  format TEXT NOT NULL,
  conversation_id TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
  title,
  text,
  content='documents',
  content_rowid='id',
  tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO documents_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
"""


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif tag in {"br", "p", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def normalize_text(value: str) -> str:
    value = value.replace("\x00", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def text_from_html(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value)
    return normalize_text(html.unescape("".join(parser.parts)))


def text_from_json(value: str) -> str:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return normalize_text(value)
    return normalize_text(json.dumps(data, ensure_ascii=False, indent=2))


def decode_document(data: bytes, suffix: str) -> str:
    value = data.decode("utf-8", errors="replace")
    if suffix == ".html":
        return text_from_html(value)
    if suffix == ".json":
        return text_from_json(value)
    return normalize_text(value)


def iter_legacy_documents(archive_root: Path) -> Iterable[tuple[str, str, str, str]]:
    bulk_root = archive_root / "exports" / "legacy-bulk-exports"
    extracted = bulk_root / "extracted-folders"
    for path in sorted(extracted.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".md", ".json", ".html", ".txt"}:
            yield (
                path.stem,
                decode_document(path.read_bytes(), path.suffix.lower()),
                str(path.relative_to(archive_root)),
                "legacy-extracted",
            )

    raw_zips = bulk_root / "raw-zips"
    for zip_path in sorted(raw_zips.glob("*.zip")):
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                suffix = Path(member.filename).suffix.lower()
                if member.is_dir() or suffix not in {".md", ".json", ".html", ".txt"}:
                    continue
                yield (
                    Path(member.filename).stem,
                    decode_document(archive.read(member), suffix),
                    f"{zip_path.relative_to(archive_root)}::{member.filename}",
                    "legacy-zip",
                )

    standalone = archive_root / "exports" / "single-conversations" / "legacy-standalone"
    for path in sorted(standalone.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".md", ".json", ".html", ".txt"}:
            yield (
                path.stem,
                decode_document(path.read_bytes(), path.suffix.lower()),
                str(path.relative_to(archive_root)),
                "legacy-standalone",
            )


def iter_older_only_official(
    archive_root: Path, current_ids: set[str]
) -> Iterable[tuple[str, str, str, str, str]]:
    history_root = archive_root / "exports" / "text-history"
    for path in sorted(history_root.glob("*/conversations*.json")):
        with path.open(encoding="utf-8") as handle:
            for conversation in _iter_json_array(handle):
                if not isinstance(conversation, dict):
                    continue
                conversation_id = conversation.get("id") or conversation.get("conversation_id")
                if not isinstance(conversation_id, str) or conversation_id in current_ids:
                    continue
                mapping = conversation.get("mapping")
                if not isinstance(mapping, dict):
                    continue
                parts: list[str] = []
                for _, _, message in _iter_messages_all_nodes(mapping, include_hidden=False):
                    author = message.get("author") or {}
                    role = author.get("role") if isinstance(author, dict) else "unknown"
                    _, text = _extract_message_text(message)
                    if text:
                        parts.append(f"## {str(role).upper()}\n{text}")
                title = str(conversation.get("title") or "Untitled")
                yield (
                    title,
                    normalize_text("\n\n".join(parts)),
                    str(path.relative_to(archive_root)),
                    "older-official-only",
                    conversation_id,
                )


def insert_document(
    connection: sqlite3.Connection,
    *,
    title: str,
    text: str,
    source_path: str,
    source_kind: str,
    format_name: str,
    conversation_id: str | None = None,
) -> bool:
    if not text:
        return False
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO documents(
          content_hash, title, text, source_path, source_kind, format, conversation_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (content_hash, title, text, source_path, source_kind, format_name, conversation_id),
    )
    return cursor.rowcount > 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a separate, content-deduplicated index of legacy ChatGPT exports."
    )
    parser.add_argument(
        "--archive-root",
        default=None,
        help="ChatGPT Archive root (default: configured archive root)",
    )
    parser.add_argument("--current-db", default=default_db_path())
    parser.add_argument("--db", default=default_historical_db_path())
    rebuild_mode = parser.add_mutually_exclusive_group()
    rebuild_mode.add_argument(
        "--reset",
        dest="reset",
        action="store_true",
        help="Replace the historical index (default)",
    )
    rebuild_mode.add_argument(
        "--incremental",
        dest="reset",
        action="store_false",
        help="Add new content without removing stale documents",
    )
    parser.set_defaults(reset=True)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    archive_root = (
        Path(args.archive_root).expanduser()
        if args.archive_root
        else configured_archive_root()
    )
    database = Path(args.db)
    database.parent.mkdir(parents=True, exist_ok=True)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            database.with_name(database.name + suffix).unlink(missing_ok=True)

    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA)
        with sqlite3.connect(args.current_db) as current_connection:
            current_ids = {
                row[0]
                for row in current_connection.execute(
                    "SELECT conversation_id FROM conversations"
                )
            }
        inserted = 0
        scanned = 0
        for title, text, source_path, source_kind in iter_legacy_documents(archive_root):
            scanned += 1
            inserted += insert_document(
                connection,
                title=title,
                text=text,
                source_path=source_path,
                source_kind=source_kind,
                format_name=Path(source_path.split("::")[-1]).suffix.lower().lstrip("."),
            )
        for title, text, source_path, source_kind, conversation_id in iter_older_only_official(
            archive_root, current_ids
        ):
            scanned += 1
            inserted += insert_document(
                connection,
                title=title,
                text=text,
                source_path=source_path,
                source_kind=source_kind,
                format_name="official-json",
                conversation_id=conversation_id,
            )
        connection.commit()
        total = connection.execute("SELECT count(*) FROM documents").fetchone()[0]
    print(f"scanned {scanned} documents; indexed {inserted} new unique documents ({total} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
