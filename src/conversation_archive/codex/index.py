#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .session_index import index_sessions
from .paths import default_db_path, default_input_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index local Codex JSONL sessions into a separate SQLite FTS database.")
    parser.add_argument("--input", default=default_input_path(), help="Codex sessions directory or one JSONL session")
    parser.add_argument("--db", default=default_db_path(), help="Separate Codex session SQLite database")
    parser.add_argument("--reset", action="store_true", help="Discard and rebuild the Codex session database")
    parser.add_argument("--no-incremental", action="store_true", help="Reindex every source file without deleting the database")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N source files")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    indexed, skipped, messages = index_sessions(
        Path(args.input).expanduser(),
        Path(args.db).expanduser(),
        reset=args.reset,
        incremental=not args.no_incremental,
        limit=args.limit,
    )
    print(f"done: indexed {indexed} sessions, skipped {skipped} unchanged sessions, retained {messages} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
