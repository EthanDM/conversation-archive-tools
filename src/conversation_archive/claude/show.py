from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from ..cli import truncate_text
from .index import open_readonly
from .paths import default_db_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Display an exported Claude conversation without selecting a branch.")
    parser.add_argument("--db", default=default_db_path())
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--conv", help="Conversation UUID")
    selector.add_argument("--title", help="Title substring to list")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--max-chars", type=int, default=None, help="Maximum characters per message")
    args = parser.parse_args(argv)
    if args.max_chars is not None and args.max_chars < 1:
        parser.error("max-chars must be positive")
    if args.title and not args.list:
        parser.error("use --title with --list, then select --conv")
    if args.conv and args.list:
        parser.error("--list requires --title")
    try:
        connection = open_readonly(Path(args.db))
        try:
            if args.title is not None:
                rows = connection.execute("SELECT * FROM conversations WHERE instr(lower(title), lower(?)) > 0 ORDER BY updated_at DESC, uuid", (args.title,)).fetchall()
                for row in rows:
                    print(f"- conv={row['uuid']} updated={row['updated_at'] or ''} title={row['title']}\n  source: {row['source']}")
                if not rows:
                    print("no conversations matched", file=sys.stderr)
                    return 1
                return 0
            conversation = connection.execute("SELECT * FROM conversations WHERE uuid=?", (args.conv,)).fetchone()
            if conversation is None:
                print("conversation not found", file=sys.stderr)
                return 1
            print(f"# {conversation['title']}\n\n- conversation: {conversation['uuid']}\n- source: {conversation['source']}\n")
            for row in connection.execute("SELECT * FROM messages WHERE conversation_uuid=? ORDER BY seq", (args.conv,)):
                print(f"## [{row['seq']}] {row['role']} ({row['created_at'] or ''})\n")
                print(f"message: {row['uuid']}")
                if row['parent_uuid']:
                    print(f"parent: {row['parent_uuid']}")
                print("\n" + truncate_text(row['text'], args.max_chars) + "\n")
        finally:
            connection.close()
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Claude transcript failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
