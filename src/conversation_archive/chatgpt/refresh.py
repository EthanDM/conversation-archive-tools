"""Atomically rebuild and activate a configured ChatGPT export index."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from .paths import (
    configured_archive_root,
    default_db_path,
    default_export_path,
    load_config,
)
from .index import index_export
from .conversation_terms import build_terms
from .reflection_indexes import build_indexes


def validate_database(path: Path) -> tuple[int, int]:
    """Verify a completed index has conversations and a one-to-one FTS message mirror.

    Raises ``RuntimeError`` when activation would expose an empty or inconsistent
    database. Returns conversation and message counts on success.
    """
    with sqlite3.connect(path) as connection:
        conversations = connection.execute("SELECT count(*) FROM conversations").fetchone()[0]
        messages = connection.execute("SELECT count(*) FROM messages").fetchone()[0]
        fts_messages = connection.execute("SELECT count(*) FROM messages_fts").fetchone()[0]
    if conversations <= 0 or messages <= 0 or fts_messages != messages:
        raise RuntimeError(
            f"Validation failed: conversations={conversations}, messages={messages}, "
            f"fts_messages={fts_messages}"
        )
    return conversations, messages


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild and atomically activate the configured ChatGPT export index."
    )
    parser.add_argument("--input", default=default_export_path())
    parser.add_argument("--db", default=default_db_path())
    parser.add_argument(
        "--archive-snapshot",
        action="store_true",
        help="Also replace the versioned index snapshot in the archive",
    )
    parser.add_argument(
        "--skip-derived",
        action="store_true",
        help="Skip reflection and related-conversation indexes",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input).expanduser()
    runtime_db = Path(args.db).expanduser()
    temporary_db = runtime_db.with_suffix(runtime_db.suffix + ".building")
    temporary_db.parent.mkdir(parents=True, exist_ok=True)

    index_export(
        input_path=input_path,
        db_path=temporary_db,
        include_hidden=False,
        write_markdown_dir=None,
        limit=None,
        progress_every=100,
        reset=True,
        all_nodes=True,
        incremental=False,
    )
    if not args.skip_derived:
        build_indexes(temporary_db, reset=True)
        build_terms(temporary_db, max_terms=200, reset=True)
    conversations, messages = validate_database(temporary_db)
    for suffix in ("-wal", "-shm"):
        temporary_db.with_name(temporary_db.name + suffix).unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        runtime_db.with_name(runtime_db.name + suffix).unlink(missing_ok=True)
    os.replace(temporary_db, runtime_db)

    if args.archive_snapshot:
        config = load_config()
        version = config.get("current_export")
        if not version:
            raise RuntimeError("current_export is missing from configuration")
        archive_root = configured_archive_root()
        snapshot = archive_root / "indexes" / f"{version}.sqlite"
        snapshot_temporary = snapshot.with_suffix(".sqlite.tmp")
        shutil.copy2(runtime_db, snapshot_temporary)
        os.replace(snapshot_temporary, snapshot)
        current_temporary = archive_root / "indexes" / "CURRENT.tmp"
        current_temporary.write_text(f"{version}.sqlite\n", encoding="utf-8")
        os.replace(current_temporary, archive_root / "indexes" / "CURRENT")

    print(
        f"activated {runtime_db}: {conversations} conversations, {messages} messages"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
