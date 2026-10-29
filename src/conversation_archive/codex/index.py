#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .session_index import index_sessions
from .source_roots import local_additional_roots, protected_local_roots
from .paths import default_db_path, default_input_path, default_shared_input_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index local Codex JSONL sessions into a separate SQLite FTS database.")
    parser.add_argument("--input", help="Codex sessions directory or one JSONL session")
    parser.add_argument(
        "--shared",
        action="store_true",
        help="Index the configured shared Codex-session archive instead of local sessions",
    )
    parser.add_argument("--include-local", action="store_true", help="With --shared, include current local sessions without publishing them")
    parser.add_argument("--db", default=default_db_path(), help="Separate Codex session SQLite database")
    parser.add_argument("--reset", action="store_true", help="Discard and rebuild the Codex session database")
    parser.add_argument("--no-incremental", action="store_true", help="Reindex every source file without deleting the database")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N source files")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.shared and args.input:
        parser.error("--shared cannot be combined with --input")
    if args.include_local and not args.shared:
        parser.error("--include-local requires --shared")
    input_path = args.input or (default_shared_input_path() if args.shared else default_input_path())
    db_path = Path(args.db).expanduser()
    additional_inputs = ()
    protected_missing_roots = ()
    if args.include_local:
        local = Path(default_input_path()).expanduser()
        local_extra = local_additional_roots(local, db_path, reset=args.reset)
        additional_inputs = (local, *local_extra)
        protected_missing_roots = protected_local_roots(local, local_extra)
    elif not args.shared and not args.input:
        local = Path(input_path).expanduser().resolve()
        additional_inputs = local_additional_roots(local, db_path, preserve_shared=True, reset=args.reset)
        protected_missing_roots = protected_local_roots(local, additional_inputs)
    indexed, skipped, messages = index_sessions(
        Path(input_path).expanduser(),
        db_path,
        reset=args.reset,
        incremental=not args.no_incremental,
        limit=args.limit,
        additional_inputs=additional_inputs,
        preserve_scope=not args.reset,
        protected_missing_roots=protected_missing_roots,
    )
    print(f"done: indexed {indexed} sessions, skipped {skipped} unchanged sessions, retained {messages} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
