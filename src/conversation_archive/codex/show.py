#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .paths import default_db_path
from ..cli import connect_sqlite, truncate_text


def show_session(db_path: Path, *, session_id: str | None, title_contains: str | None, list_only: bool, max_chars: int | None) -> int:
    connection = connect_sqlite(db_path)
    if not session_id:
        if not title_contains:
            print("provide --session or --title", file=sys.stderr)
            return 2
        matches = connection.execute(
            """
            SELECT session_id, title, last_time_iso, cwd, source_path FROM sessions
            WHERE lower(title) LIKE '%' || lower(?) || '%' ORDER BY last_time_iso DESC LIMIT 50
            """,
            (title_contains,),
        ).fetchall()
        if not matches:
            print("no sessions matched", file=sys.stderr)
            return 1
        for item in matches:
            print(f"- session={item['session_id']} updated={item['last_time_iso'] or ''} title={item['title']}\n  cwd={item['cwd'] or ''}\n  source={item['source_path']}")
        return 0 if list_only else 0
    session = connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if not session:
        print("session not found", file=sys.stderr)
        return 1
    print(f"# {session['title']}\n")
    print(f"- session_id: `{session['session_id']}`")
    print(f"- cwd: `{session['cwd'] or ''}`")
    print(f"- source: `{session['source_path']}`\n")
    for item in connection.execute("SELECT seq, role, create_time_iso, text FROM messages WHERE session_id=? ORDER BY seq", (session_id,)):
        text = truncate_text(item["text"], max_chars)
        print(f"## [{item['seq']}] {item['role']} ({item['create_time_iso'] or ''})\n")
        print(text + "\n")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Print a filtered transcript from the local Codex session index.")
    parser.add_argument("--db", default=default_db_path())
    parser.add_argument("--session", default=None, help="Session ID to print")
    parser.add_argument("--title", default=None, help="Search generated title substring")
    parser.add_argument("--list", action="store_true", help="List title matches without selecting a session")
    parser.add_argument("--max-chars", type=int, default=None)
    args = parser.parse_args(argv)
    return show_session(Path(args.db).expanduser(), session_id=args.session, title_contains=args.title, list_only=args.list, max_chars=args.max_chars)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
