#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..chatgpt.paths import default_db_path

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _parse_roles(value: str) -> Set[str]:
    return {r.strip().lower() for r in value.split(",") if r.strip()}


def _truncate(text: str, max_chars: Optional[int]) -> str:
    if max_chars is None:
        return text
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _wrap(text: str, width: int) -> str:
    return "\n".join(
        textwrap.wrap(text, width=width, replace_whitespace=False, drop_whitespace=False)
    )


def _fetch_context(
    cur: sqlite3.Cursor,
    conversation_id: str,
    seq: int,
    context: int,
) -> List[sqlite3.Row]:
    start = max(1, seq - context)
    end = seq + context
    return cur.execute(
        """
        SELECT id, seq, role, create_time_iso, text
        FROM messages
        WHERE conversation_id=?
          AND seq BETWEEN ? AND ?
        ORDER BY seq ASC
        """,
        (conversation_id, start, end),
    ).fetchall()


def context_pack(
    db_path: Path,
    query: str,
    *,
    limit: int,
    context: int,
    roles: Optional[Set[str]],
    max_message_chars: Optional[int],
    max_total_chars: int,
    width: int,
    format: str,
) -> str:
    conn = _connect(db_path)
    cur = conn.cursor()

    hits = cur.execute(
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

    out_hits: List[Dict[str, object]] = []
    total_chars = 0

    for h in hits:
        hit_role = (h["role"] or "unknown").lower()
        if roles is not None and hit_role not in roles:
            continue

        ctx_rows = _fetch_context(cur, h["conversation_id"], int(h["seq"]), context)
        ctx: List[Dict[str, object]] = []
        for r in ctx_rows:
            role = (r["role"] or "unknown").lower()
            if roles is not None and role not in roles:
                continue
            text = _truncate((r["text"] or "").rstrip(), max_message_chars)
            ctx.append(
                {
                    "id": int(r["id"]),
                    "seq": int(r["seq"]),
                    "role": role,
                    "time": r["create_time_iso"] or None,
                    "text": text,
                    "is_hit": int(r["id"]) == int(h["id"]),
                }
            )

        entry: Dict[str, object] = {
            "score": float(h["score"]),
            "conversation_id": h["conversation_id"],
            "title": h["title"] or "",
            "hit": {
                "id": int(h["id"]),
                "seq": int(h["seq"]),
                "role": hit_role,
                "time": h["create_time_iso"] or None,
            },
            "context": ctx,
        }

        blob = json.dumps(entry, ensure_ascii=False)
        if max_total_chars > 0 and total_chars + len(blob) > max_total_chars and out_hits:
            break
        out_hits.append(entry)
        total_chars += len(blob)

    if format == "json":
        return json.dumps(
            {
                "query": query,
                "db": str(db_path),
                "results": out_hits,
            },
            ensure_ascii=False,
            indent=2,
        )

    # Markdown
    parts: List[str] = []
    parts.append(f"# Context pack\n")
    parts.append(f"- query: `{query}`")
    parts.append(f"- db: `{db_path}`")
    parts.append(f"- results: `{len(out_hits)}`\n")
    for entry in out_hits:
        hit = entry["hit"]  # type: ignore[assignment]
        parts.append(
            f"## {entry['title']}\n\n- conv: `{entry['conversation_id']}`\n- score: `{entry['score']}`\n- hit: `{hit['id']}` seq `{hit['seq']}` role `{hit['role']}` time `{hit['time'] or ''}`\n"
        )
        parts.append("### Context\n")
        for r in entry["context"]:  # type: ignore[assignment]
            prefix = ">>" if r["is_hit"] else "  "
            header = f"{prefix} [{r['seq']}] {r['role']}"
            if r.get("time"):
                header += f" ({r['time']})"
            parts.append(header)
            txt = r["text"] or ""
            txt = _wrap(str(txt), width=width)
            if txt:
                parts.append(txt)
            parts.append("")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description="Export top FTS hits + context as a pasteable bundle for Codex.")
    p.add_argument("query", help="SQLite FTS5 query")
    p.add_argument("--db", default=default_db_path(), help="SQLite DB path")
    p.add_argument("--limit", type=int, default=20, help="Max hits to consider (default: 20)")
    p.add_argument("--context", type=int, default=3, help="Messages before/after each hit (default: 3)")
    p.add_argument(
        "--roles",
        default=None,
        help="Comma-separated roles to include (e.g. user,assistant). Default: all roles.",
    )
    p.add_argument(
        "--max-message-chars",
        type=int,
        default=2000,
        help="Truncate each message to N chars (default: 2000)",
    )
    p.add_argument(
        "--max-total-chars",
        type=int,
        default=60000,
        help="Stop when the serialized pack exceeds N chars (default: 60000)",
    )
    p.add_argument("--width", type=int, default=100, help="Wrap width in Markdown (default: 100)")
    p.add_argument(
        "--format",
        choices=["md", "json"],
        default="md",
        help="Output format (default: md)",
    )
    p.add_argument("--out", default=None, help="Write output to a file instead of stdout")
    args = p.parse_args(argv)

    roles = _parse_roles(args.roles) if args.roles else None
    data = context_pack(
        Path(args.db),
        args.query,
        limit=args.limit,
        context=args.context,
        roles=roles,
        max_message_chars=args.max_message_chars,
        max_total_chars=args.max_total_chars,
        width=args.width,
        format=args.format,
    )

    if args.out:
        Path(args.out).write_text(data, encoding="utf-8")
        return 0
    sys.stdout.write(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
