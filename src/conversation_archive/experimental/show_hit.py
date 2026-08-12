#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Set

from ..chatgpt.paths import default_db_path
from ..cli import connect_sqlite, parse_roles, truncate_text


def show_hit(
    db_path: Path,
    *,
    message_id: int,
    context: int,
    roles: Optional[Set[str]],
    max_chars: Optional[int],
    include_time: bool,
) -> int:
    conn = connect_sqlite(db_path)
    cur = conn.cursor()

    hit = cur.execute(
        """
        SELECT m.id, m.conversation_id, m.seq, m.role, m.create_time_iso, m.text, c.title AS title
        FROM messages m
        JOIN conversations c ON c.conversation_id=m.conversation_id
        WHERE m.id=?
        """,
        (message_id,),
    ).fetchone()
    if hit is None:
        print("message not found", file=sys.stderr)
        return 1

    start_seq = max(1, int(hit["seq"]) - context)
    end_seq = int(hit["seq"]) + context

    rows = cur.execute(
        """
        SELECT id, seq, role, create_time_iso, text
        FROM messages
        WHERE conversation_id=?
          AND seq BETWEEN ? AND ?
        ORDER BY seq ASC
        """,
        (hit["conversation_id"], start_seq, end_seq),
    ).fetchall()

    print(f"# {hit['title'] or ''}\n")
    print(f"- conversation_id: `{hit['conversation_id']}`")
    print(f"- hit_message_id: `{hit['id']}`\n")

    for r in rows:
        role = (r["role"] or "unknown").lower()
        if roles is not None and role not in roles:
            continue
        prefix = ">>" if int(r["id"]) == int(hit["id"]) else "  "
        time = r["create_time_iso"] or ""
        header = f"{prefix} [{r['seq']}] {role}"
        if include_time and time:
            header += f" ({time})"
        print(header)
        print(truncate_text((r["text"] or "").rstrip(), max_chars))
        print()

    return 0


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description="Show a search hit (messages.id) with surrounding context.")
    p.add_argument("msg", type=int, help="messages.id (rowid) of the hit")
    p.add_argument("--db", default=default_db_path(), help="SQLite DB path")
    p.add_argument("--context", type=int, default=5, help="Messages before/after (default: 5)")
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
    return show_hit(
        Path(args.db),
        message_id=args.msg,
        context=args.context,
        roles=roles,
        max_chars=args.max_chars,
        include_time=not args.no_time,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
