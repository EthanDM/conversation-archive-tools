#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

from .paths import default_db_path
from ..cli import connect_sqlite, format_snippet

def search(
    db_path: Path,
    query: str,
    *,
    limit: int,
    include_text: bool,
    context: int,
    width: int,
    snippet_lines: int,
) -> int:
    conn = connect_sqlite(db_path)
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT
          m.id,
          m.conversation_id,
          c.title AS title,
          m.seq,
          m.role,
          m.create_time_iso,
          m.text,
          bm25(messages_fts) AS score
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        JOIN conversations c ON c.conversation_id = m.conversation_id
        WHERE messages_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()

    if not rows:
        print("no matches", file=sys.stderr)
        return 1

    for r in rows:
        print(
            f"- score={r['score']:.3f} conv={r['conversation_id']} msg_id={r['id']} seq={r['seq']} role={r['role']} time={r['create_time_iso'] or ''}"
        )
        print(f"  title: {r['title']}")
        if context > 0:
            ctx_rows = cur.execute(
                """
                SELECT seq, role, create_time_iso, text
                FROM messages
                WHERE conversation_id = ?
                  AND seq BETWEEN ? AND ?
                ORDER BY seq
                """,
                (r["conversation_id"], max(1, int(r["seq"]) - context), int(r["seq"]) + context),
            ).fetchall()
            print("  context:")
            for cr in ctx_rows:
                header = f"  [{cr['seq']}] {cr['role']} {cr['create_time_iso'] or ''}".rstrip()
                print(header)
                if include_text:
                    snippet = format_snippet(cr["text"] or "", width=width, max_lines=snippet_lines)
                    for line in snippet.splitlines():
                        print(f"    {line}")
                print()
        elif include_text:
            snippet = format_snippet(r['text'] or '', width=width, max_lines=snippet_lines)
            print("  ---")
            for line in snippet.splitlines():
                print(f"  {line}")
            print("  ---")
        print()
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Search a ChatGPT export SQLite FTS index created by conversation-archive-index-chatgpt."
    )
    parser.add_argument(
        "query",
        help="FTS query (supports phrases, NEAR, boolean ops); example: 'caffeine NEAR headache'",
    )
    parser.add_argument(
        "--db",
        default=default_db_path(),
        help="SQLite DB path (default: configured current database)",
    )
    parser.add_argument("--limit", type=int, default=20, help="Max hits (default: 20)")
    parser.add_argument(
        "--no-text",
        action="store_true",
        help="Do not print snippets (only metadata)",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=0,
        help="If >0, print N messages of context before/after each hit (default: 0)",
    )
    parser.add_argument("--width", type=int, default=100, help="Wrap width (default: 100)")
    parser.add_argument(
        "--lines", type=int, default=10, help="Snippet lines per hit (default: 10)"
    )
    args = parser.parse_args(argv)
    return search(
        Path(args.db),
        args.query,
        limit=args.limit,
        include_text=not args.no_text,
        context=args.context,
        width=args.width,
        snippet_lines=args.lines,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
