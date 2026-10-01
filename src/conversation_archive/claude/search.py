from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from ..cli import format_snippet, parse_roles
from .index import open_readonly
from .paths import default_db_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Search the separate local Claude FTS index.")
    parser.add_argument("query", help="FTS5 query, including phrases and boolean operators")
    parser.add_argument("--db", default=default_db_path())
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--context", type=int, default=0)
    parser.add_argument("--role", default=None, help="Comma-separated user,assistant roles")
    parser.add_argument("--width", type=int, default=100)
    parser.add_argument("--lines", type=int, default=10)
    args = parser.parse_args(argv)
    roles = parse_roles(args.role)
    if args.limit < 1 or args.context < 0 or args.width < 1 or args.lines < 1:
        parser.error("limit, width, and lines must be positive; context must be nonnegative")
    if roles is not None and (not roles or not roles <= {"user", "assistant"}):
        parser.error("role must contain user and/or assistant")
    try:
        connection = open_readonly(Path(args.db))
        try:
            sql = """SELECT m.*, c.title, c.source, bm25(messages_fts) AS score
                     FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid
                     JOIN conversations c ON c.uuid=m.conversation_uuid
                     WHERE messages_fts MATCH ?"""
            params = [args.query]
            if roles:
                ordered_roles = sorted(roles)
                sql += " AND m.role IN (" + ",".join("?" for _ in ordered_roles) + ")"
                params.extend(ordered_roles)
            sql += " ORDER BY score, m.id LIMIT ?"
            params.append(args.limit)
            rows = connection.execute(sql, params).fetchall()
            if not rows:
                print("no matches", file=sys.stderr)
                return 1
            for row in rows:
                print(f"- score={row['score']:.3f} conv={row['conversation_uuid']} message={row['uuid']} seq={row['seq']} role={row['role']} time={row['created_at'] or ''}")
                print(f"  title: {row['title']}\n  source: {row['source']}")
                context = connection.execute(
                    "SELECT seq, role, created_at, text FROM messages WHERE conversation_uuid=? AND seq BETWEEN ? AND ? ORDER BY seq",
                    (row['conversation_uuid'], row['seq'] - args.context, row['seq'] + args.context),
                ).fetchall()
                for item in context:
                    print(f"  [{item['seq']}] {item['role']} {item['created_at'] or ''}")
                    print(format_snippet(item['text'], width=args.width, max_lines=args.lines))
                print()
        finally:
            connection.close()
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Claude search failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
