"""Command-line incremental import of local Claude Code sessions."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .paths import default_db_path, default_input_path
from .session_index import index_sessions


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=default_input_path())
    parser.add_argument("--db", default=default_db_path())
    parser.add_argument("--no-incremental", action="store_true")
    args = parser.parse_args(argv)
    try:
        indexed, skipped, messages = index_sessions(Path(args.input), Path(args.db), incremental=not args.no_incremental)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Claude Code import failed: {error}", file=sys.stderr)
        return 1
    print(f"done: indexed {indexed} sessions, skipped {skipped} unchanged sessions, retained {messages} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
