"""Export selected indexed conversations into reviewable local handoff bundles."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from .paths import default_db_path, default_output_dir


def export_handoff(db_path: Path, conversation_ids: list[str], output_path: Path, *, output_format: str, redactions: list[str]) -> int:
    """Write selected conversations as Markdown or JSON after optional regex redaction.

    Missing conversation IDs and invalid regex patterns raise errors. Redaction is
    applied before writing, but the caller remains responsible for reviewing the
    output before sharing it.
    """
    patterns = [re.compile(pattern, re.I) for pattern in redactions]
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        conversations = []
        for conversation_id in conversation_ids:
            conversation = connection.execute("SELECT conversation_id, title FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if conversation is None:
                raise ValueError(f"conversation not found: {conversation_id}")
            messages = []
            for row in connection.execute("SELECT role, create_time_iso, text FROM messages WHERE conversation_id=? ORDER BY seq", (conversation_id,)):
                text = row["text"] or ""
                for pattern in patterns:
                    text = pattern.sub("[REDACTED]", text)
                messages.append({"role": row["role"], "time": row["create_time_iso"], "text": text})
            conversations.append({"conversation_id": conversation["conversation_id"], "title": conversation["title"], "messages": messages})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        output_path.write_text(json.dumps({"conversations": conversations, "redactions": redactions}, indent=2) + "\n", encoding="utf-8")
    else:
        lines = ["# Selected conversation handoff", "", "Review before sharing. Source conversation IDs are retained below.", ""]
        for conversation in conversations:
            lines.extend([f"## {conversation['title'] or 'Untitled'}", "", f"- conversation_id: `{conversation['conversation_id']}`", ""])
            for message in conversation["messages"]:
                lines.extend([f"### {message['role'] or 'unknown'}", "", message["text"], ""])
        output_path.write_text("\n".join(lines), encoding="utf-8")
    return len(conversations)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Export selected indexed conversations as a reviewable handoff bundle.")
    parser.add_argument("--db", default=default_db_path())
    parser.add_argument("--conv", action="append", required=True, help="Conversation ID to include; repeat for multiple conversations")
    parser.add_argument("--out", default=str(default_output_dir() / "selected-conversations.md"))
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--redact", action="append", default=[], help="Case-insensitive regex to replace with [REDACTED]; repeat as needed")
    args = parser.parse_args(argv)
    count = export_handoff(Path(args.db).expanduser(), args.conv, Path(args.out).expanduser(), output_format=args.format, redactions=args.redact)
    print(f"wrote {count} conversation(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
