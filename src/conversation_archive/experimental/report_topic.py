#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
import textwrap
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import DefaultDict, List, Optional, Tuple

from ..chatgpt.paths import default_db_path

@dataclass
class Hit:
    conversation_id: str
    title: str
    create_time_iso: Optional[str]
    seq: int
    role: str
    text: str
    score: float


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _snippet(text: str, width: int, max_lines: int) -> str:
    wrapped = textwrap.wrap(text, width=width, replace_whitespace=False, drop_whitespace=False)
    if len(wrapped) > max_lines:
        wrapped = wrapped[:max_lines] + ["…"]
    return "\n".join(wrapped).strip()


def report(
    db_path: Path,
    query: str,
    *,
    max_hits: int,
    max_conversations: int,
    per_conversation: int,
    width: int,
    lines: int,
    sort_by: str,
) -> int:
    conn = _connect(db_path)
    cur = conn.cursor()

    rows = cur.execute(
        """
        SELECT
          m.conversation_id,
          c.title AS title,
          m.create_time_iso,
          m.seq,
          m.role,
          m.text,
          bm25(messages_fts) AS score
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        JOIN conversations c ON c.conversation_id = m.conversation_id
        WHERE messages_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (query, max_hits),
    ).fetchall()

    if not rows:
        print("no matches", file=sys.stderr)
        return 1

    hits: List[Hit] = []
    for r in rows:
        hits.append(
            Hit(
                conversation_id=r["conversation_id"],
                title=r["title"] or "",
                create_time_iso=r["create_time_iso"],
                seq=int(r["seq"] or 0),
                role=r["role"] or "unknown",
                text=r["text"] or "",
                score=float(r["score"]),
            )
        )

    by_conv: DefaultDict[str, List[Hit]] = defaultdict(list)
    for h in hits:
        by_conv[h.conversation_id].append(h)

    conv_summaries: List[Tuple[str, str, Optional[str], float, int]] = []
    for cid, hs in by_conv.items():
        hs.sort(key=lambda x: x.score)
        best_score = hs[0].score
        earliest = min((h.create_time_iso for h in hs if h.create_time_iso), default=None)
        conv_summaries.append((cid, hs[0].title, earliest, best_score, len(hs)))

    if sort_by == "time":
        conv_summaries.sort(key=lambda x: (x[2] or "9999", x[3]))
    else:
        conv_summaries.sort(key=lambda x: x[3])

    conv_summaries = conv_summaries[:max_conversations]

    for cid, title, earliest, best_score, count in conv_summaries:
        print(f"- time={earliest or ''} hits={count} best_score={best_score:.3f}")
        print(f"  conv: {cid}")
        print(f"  title: {title}")
        # show top snippets for this conversation by score
        hs = sorted(by_conv[cid], key=lambda x: x.score)[:per_conversation]
        for h in hs:
            print(f"  [{h.seq}] {h.role} {h.create_time_iso or ''} score={h.score:.3f}".rstrip())
            sn = _snippet(h.text, width=width, max_lines=lines)
            for line in sn.splitlines():
                print(f"    {line}")
        print()

    return 0


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(
        description="Group FTS hits by conversation (useful for 'when did I talk about X, and about what?')."
    )
    p.add_argument("query", help="SQLite FTS5 query")
    p.add_argument("--db", default=default_db_path(), help="SQLite DB path")
    p.add_argument("--max-hits", type=int, default=2000, help="Max raw hits to consider (default: 2000)")
    p.add_argument(
        "--max-conversations",
        type=int,
        default=50,
        help="Max conversations to print (default: 50)",
    )
    p.add_argument(
        "--per-conversation",
        type=int,
        default=3,
        help="Snippets per conversation (default: 3)",
    )
    p.add_argument("--width", type=int, default=100, help="Wrap width (default: 100)")
    p.add_argument("--lines", type=int, default=6, help="Lines per snippet (default: 6)")
    p.add_argument(
        "--sort",
        choices=["time", "score"],
        default="time",
        help="Sort conversations by earliest match time or best score (default: time)",
    )
    args = p.parse_args(argv)
    return report(
        Path(args.db),
        args.query,
        max_hits=args.max_hits,
        max_conversations=args.max_conversations,
        per_conversation=args.per_conversation,
        width=args.width,
        lines=args.lines,
        sort_by=args.sort,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
