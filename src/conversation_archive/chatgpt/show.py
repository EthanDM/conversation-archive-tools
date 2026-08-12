#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Set

from .paths import default_db_path
from ..cli import connect_sqlite, parse_roles, truncate_text


def _print_markdown(
    *,
    title: str,
    conversation_id: str,
    rows: Sequence[sqlite3.Row],
    roles: Optional[Set[str]],
    max_chars: Optional[int],
    include_time: bool,
) -> None:
    print(f"# {title}\n")
    print(f"- conversation_id: `{conversation_id}`\n")
    for r in rows:
        role = (r["role"] or "unknown").lower()
        if roles is not None and role not in roles:
            continue
        seq = r["seq"]
        time = r["create_time_iso"] or ""
        header = f"## [{seq}] {role}"
        if include_time and time:
            header += f" ({time})"
        print(header)
        print()
        print(truncate_text((r["text"] or "").rstrip(), max_chars))
        print()


def show_conversation(
    db_path: Path,
    *,
    conversation_id: Optional[str],
    title_contains: Optional[str],
    roles: Optional[Set[str]],
    max_chars: Optional[int],
    include_time: bool,
    list_only: bool,
) -> int:
    conn = connect_sqlite(db_path)
    cur = conn.cursor()

    if conversation_id is None:
        if not title_contains:
            print("provide --conv or --title", file=sys.stderr)
            return 2
        matches = cur.execute(
            """
            SELECT conversation_id, title, create_time_iso, update_time_iso
            FROM conversations
            WHERE lower(title) LIKE '%' || lower(?) || '%'
            ORDER BY update_time DESC
            LIMIT 50
            """,
            (title_contains,),
        ).fetchall()
        if not matches:
            print("no conversations matched", file=sys.stderr)
            return 1
        for m in matches:
            print(
                f"- conv={m['conversation_id']} updated={m['update_time_iso'] or ''} title={m['title'] or ''}"
            )
        if list_only:
            return 0
        print("\nPick a conversation_id above and re-run with --conv.", file=sys.stderr)
        return 0

    conv = cur.execute(
        "SELECT conversation_id, title FROM conversations WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    if conv is None:
        print("conversation not found", file=sys.stderr)
        return 1

    rows = cur.execute(
        """
        SELECT id, seq, role, create_time_iso, text
        FROM messages
        WHERE conversation_id=?
        ORDER BY seq ASC
        """,
        (conversation_id,),
    ).fetchall()

    _print_markdown(
        title=conv["title"] or "",
        conversation_id=conversation_id,
        rows=rows,
        roles=roles,
        max_chars=max_chars,
        include_time=include_time,
    )
    return 0


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description="Print a conversation transcript from the local ChatGPT index.")
    p.add_argument("--db", default=default_db_path(), help="SQLite DB path")
    p.add_argument("--conv", default=None, help="conversation_id to print")
    p.add_argument("--title", default=None, help="Search conversations by title substring")
    p.add_argument("--list", action="store_true", help="When using --title, only list matches")
    p.add_argument(
        "--roles",
        default=None,
        help="Comma-separated roles to include (e.g. user,assistant). Default: all roles.",
    )
    p.add_argument(
        "--max-chars",
        type=int,
        default=None,
        help="Truncate each message to N chars (default: no truncation)",
    )
    p.add_argument(
        "--no-time",
        action="store_true",
        help="Do not print timestamps in headers",
    )
    args = p.parse_args(argv)
    roles = parse_roles(args.roles)
    return show_conversation(
        Path(args.db),
        conversation_id=args.conv,
        title_contains=args.title,
        roles=roles,
        max_chars=args.max_chars,
        include_time=not args.no_time,
        list_only=bool(args.list),
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except BrokenPipeError:
        # Allow piping to `head`/`sed` without stacktraces.
        raise SystemExit(0)
