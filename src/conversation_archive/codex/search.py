#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import closing
import sys
import sqlite3
from pathlib import Path

from .paths import default_db_path, default_input_path, default_shared_input_path
from .session_index import index_sessions
from ..cli import format_snippet


def search(db_path: Path, query: str, *, limit: int, context: int, width: int, lines: int) -> int:
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT m.id, m.session_id, m.seq, m.role, m.create_time_iso, m.text,
                   s.title, s.cwd, s.source_path, bm25(messages_fts) AS score
            FROM messages_fts
            JOIN messages m ON m.id=messages_fts.rowid
            JOIN sessions s ON s.session_id=m.session_id
            WHERE messages_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        if not rows:
            print("no matches", file=sys.stderr)
            return 1
        for row in rows:
            print(f"- score={row['score']:.3f} session={row['session_id']} msg_id={row['id']} seq={row['seq']} role={row['role']} time={row['create_time_iso'] or ''}")
            print(f"  title: {row['title']}")
            print(f"  cwd: {row['cwd'] or ''}")
            print(f"  source: {row['source_path']}")
            if context:
                context_rows = connection.execute(
                    """
                    SELECT seq, role, create_time_iso, text FROM messages
                    WHERE session_id=? AND seq BETWEEN ? AND ? ORDER BY seq
                    """,
                    (row["session_id"], max(1, row["seq"] - context), row["seq"] + context),
                ).fetchall()
                print("  context:")
                for item in context_rows:
                    print(f"  [{item['seq']}] {item['role']} {item['create_time_iso'] or ''}".rstrip())
                    for line in format_snippet(item["text"], width=width, max_lines=lines).splitlines():
                        print(f"    {line}")
            else:
                print("  ---")
                for line in format_snippet(row["text"], width=width, max_lines=lines).splitlines():
                    print(f"  {line}")
                print("  ---")
            print()
        return 0

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Search the separate local Codex session SQLite FTS index.")
    parser.add_argument("query", help="FTS query; supports phrases and boolean operators")
    parser.add_argument("--db", default=None, help="Codex session SQLite database")
    parser.add_argument("--input", help="Session source directory or JSONL file to refresh")
    parser.add_argument("--no-refresh", action="store_true", help="Search the existing snapshot without writing to the index")
    parser.add_argument("--shared", action="store_true", help="Refresh from the configured shared Codex archive")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--context", type=int, default=0, help="Messages before/after each hit")
    parser.add_argument("--width", type=int, default=100)
    parser.add_argument("--lines", type=int, default=10)
    args = parser.parse_args(argv)
    db_path = Path(args.db or default_db_path()).expanduser()
    if args.no_refresh and args.input:
        parser.error("--input cannot be combined with --no-refresh")
    if args.shared and (args.input or args.no_refresh):
        parser.error("--shared cannot be combined with --input or --no-refresh")
    if args.db and not args.input and not args.shared and not args.no_refresh:
        parser.error("custom --db requires --input or --no-refresh")
    try:
        if not args.no_refresh:
            input_path = Path(args.input or (default_shared_input_path() if args.shared else default_input_path())).expanduser()
            # Codex's indexer predates provider guards; avoid mutating another provider.
            if db_path.exists():
                with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                    metadata = dict(connection.execute("SELECT key, value FROM index_meta"))
                if metadata.get("provider") not in {None, "codex"} or "indexer_version" not in metadata:
                    raise ValueError("Database is not a supported Codex session index")
            index_sessions(input_path, db_path)
        return search(db_path, args.query, limit=args.limit, context=args.context, width=args.width, lines=args.lines)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Codex search failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
