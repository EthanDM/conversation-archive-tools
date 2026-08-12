from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .paths import default_historical_db_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Search the separate legacy ChatGPT index.")
    parser.add_argument("query", help="SQLite FTS5 query")
    parser.add_argument("--db", default=default_historical_db_path())
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    with sqlite3.connect(Path(args.db)) as connection:
        rows = connection.execute(
            """
            SELECT d.title, d.source_kind, d.source_path,
                   snippet(documents_fts, 1, '[', ']', ' … ', 32),
                   bm25(documents_fts)
            FROM documents_fts
            JOIN documents d ON d.id = documents_fts.rowid
            WHERE documents_fts MATCH ?
            ORDER BY bm25(documents_fts)
            LIMIT ?
            """,
            (args.query, args.limit),
        ).fetchall()
    for title, source_kind, source_path, snippet, score in rows:
        print(f"- score={score:.3f} [{source_kind}] {title}")
        print(f"  source: {source_path}")
        print(f"  {snippet}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
