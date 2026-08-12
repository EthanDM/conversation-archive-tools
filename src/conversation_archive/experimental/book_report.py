#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ..chatgpt.paths import default_db_path, default_output_dir

DEFAULT_QUERY = (
    "book AND (write OR writing OR create OR draft OR drafting OR outline OR manuscript OR novel OR "
    "publish OR publishing OR chapter OR chapters OR proposal)"
)


@dataclass
class Mention:
    create_time_iso: Optional[str]
    conversation_id: str
    title: str
    message_rowid: int
    seq: int
    text: str


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _score_excerpt(text: str) -> int:
    t = text.strip().lower()
    score = 0
    if "thesis:" in t:
        score += 100
    if "title:" in t or "working title" in t:
        score += 50
    if "outline" in t:
        score += 20
    if "chapter" in t:
        score += 10
    if "novel" in t or "manuscript" in t:
        score += 10
    # Prefer substantive passages.
    n = len(text.strip())
    if 200 <= n <= 2000:
        score += 25
    elif n > 2000:
        score += 10
    elif n < 80:
        score -= 20
    # Downweight obvious non-writing “book” contexts if they slip in.
    if "book recommendation" in t or "children's book" in t:
        score -= 50
    if "book a" in t or "booking" in t:
        score -= 30
    return score


def _looks_like_code_or_log(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    lowered = t.lower()
    if "diff --git" in lowered or lowered.startswith("diff --git"):
        return True
    if lowered.startswith("export const ") or lowered.startswith("import "):
        return True
    if "from '@" in lowered or "from \"@" in lowered:
        return True
    # Heuristic: lots of typical code punctuation compared to letters.
    punct = sum(t.count(ch) for ch in "{}[]();<>=$`")
    letters = sum(c.isalpha() for c in t)
    if letters > 0 and punct / max(1, letters) > 0.25:
        return True
    return False


def _looks_like_transcript_or_web_paste(text: str) -> bool:
    t = text.strip()
    lowered = t.lower()
    if lowered.startswith("transcript"):
        return True
    if lowered.startswith("skip to main content"):
        return True
    if "sign in" in lowered and "newsletter" in lowered and "search" in lowered:
        return True
    if "subscribers" in lowered and "views" in lowered and "share" in lowered:
        return True
    if "youtube" in lowered and "subscribed" in lowered:
        return True
    # Heuristic: lots of timestamps like 0:34 33:39 1:02 etc.
    ts_hits = len(re.findall(r"\b\d{1,2}:\d{2}\b", lowered[:1000]))
    if ts_hits >= 6:
        return True
    # Heuristic: looks like scraped HTML text.
    if "<html" in lowered or "<div" in lowered or "</" in lowered:
        return True
    return False


def _looks_like_first_person_book_writing(text: str) -> bool:
    lowered = text.lower()
    # Direct phrases we care about.
    if "write a book" in lowered or "writing a book" in lowered:
        return True
    if "my first book" in lowered:
        return True
    if (
        "book idea" in lowered
        or "book outline" in lowered
        or "book proposal" in lowered
        or "book draft" in lowered
    ):
        return True
    if "book writing" in lowered:
        return True
    if "create a book" in lowered or "create my book" in lowered or "create this book" in lowered:
        return True
    # First-person + (write|writing) + book somewhere.
    if re.search(r"\b(i|i'm|im|i’d|i'd|i would|i want to|i think|i could|can i|should i)\b", lowered):
        if "book" in lowered and (
            re.search(r"\bwrite\b", lowered)
            or "writing" in lowered
            or "create" in lowered
            or "draft" in lowered
            or "outline" in lowered
            or "publish" in lowered
        ):
            return True
    return False


def _exclude_scheduling_or_reading_list(text: str) -> bool:
    lowered = text.lower()
    # Avoid cases where 'book' is primarily "book it / booking" or "book list" rather than authorship.
    if (
        "booking" in lowered
        or "booked" in lowered
        or "book it" in lowered
        or "book a " in lowered
        or "book under" in lowered
    ):
        if (
            "write a book" not in lowered
            and "writing a book" not in lowered
            and "book writing" not in lowered
            and "book outline" not in lowered
            and "book draft" not in lowered
            and "book proposal" not in lowered
        ):
            return True
    if "book list" in lowered and "write a book" not in lowered and "writing a book" not in lowered:
        return True
    return False


def _first_paragraph(text: str, max_chars: int) -> str:
    text = text.strip()
    if not text:
        return ""
    # Prefer the first non-empty paragraph.
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    head = ""
    for p in parts:
        if re.fullmatch(r"[\u2014\u2e3a\\-\\s]+", p):  # dashes/separators
            continue
        head = p
        break
    if not head:
        head = parts[0] if parts else text
    head = re.sub(r"\s+", " ", head).strip()
    if len(head) > max_chars:
        head = head[: max_chars - 1].rstrip() + "…"
    return head


def _candidate_rows_like(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.cursor()
    # Broad net; refined later with heuristics.
    return cur.execute(
        """
        SELECT
          m.create_time_iso,
          m.conversation_id,
          c.title AS title,
          m.id AS message_rowid,
          m.seq,
          m.text
        FROM messages m
        JOIN conversations c ON c.conversation_id = m.conversation_id
        WHERE m.role = 'user'
          AND m.content_type IN ('text', 'multimodal_text')
          -- NOTE: substring match; this will match \"facebook\"/\"macbook\". Prefer --mode fts when possible.
          AND lower(m.text) LIKE '%book%'
          AND (
            lower(m.text) LIKE '%writ%'
            OR lower(m.text) LIKE '%create%'
            OR lower(m.text) LIKE '%draft%'
            OR lower(m.text) LIKE '%outline%'
            OR lower(m.text) LIKE '%manuscript%'
            OR lower(m.text) LIKE '%novel%'
            OR lower(m.text) LIKE '%publish%'
            OR lower(m.text) LIKE '%chapter%'
          )
        ORDER BY m.create_time ASC, m.id ASC
        """
    ).fetchall()


def find_mentions(db_path: Path, query: str, *, mode: str) -> List[Mention]:
    conn = _connect(db_path)
    if mode == "fts":
        if not query.strip():
            raise SystemExit("--mode fts requires a non-empty --query")
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT
              m.create_time_iso,
              m.conversation_id,
              c.title AS title,
              m.id AS message_rowid,
              m.seq,
              m.text
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN conversations c ON c.conversation_id = m.conversation_id
            WHERE messages_fts MATCH ?
              AND m.role = 'user'
              AND m.content_type IN ('text', 'multimodal_text')
            ORDER BY m.create_time ASC, m.id ASC
            """,
            (query,),
        ).fetchall()
    else:
        rows = _candidate_rows_like(conn)

    mentions: List[Mention] = []
    for r in rows:
        text = r["text"] or ""
        if _looks_like_code_or_log(text):
            continue
        if _looks_like_transcript_or_web_paste(text):
            continue
        if _exclude_scheduling_or_reading_list(text):
            continue
        if not _looks_like_first_person_book_writing(text):
            continue
        mentions.append(
            Mention(
                create_time_iso=r["create_time_iso"],
                conversation_id=r["conversation_id"],
                title=r["title"] or "",
                message_rowid=int(r["message_rowid"]),
                seq=int(r["seq"] or 0),
                text=text,
            )
        )
    return mentions


def write_outputs(
    mentions: List[Mention],
    *,
    out_dir: Path,
    csv_name: str,
    md_name: str,
    grouped_md_name: str,
) -> Tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / csv_name
    md_path = out_dir / md_name
    grouped_md_path = out_dir / grouped_md_name

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "create_time_iso",
                "conversation_id",
                "title",
                "message_rowid",
                "seq",
                "text",
            ]
        )
        for m in mentions:
            w.writerow([m.create_time_iso or "", m.conversation_id, m.title, m.message_rowid, m.seq, m.text])

    with md_path.open("w", encoding="utf-8") as f:
        f.write("# Book-writing mentions (message-level)\n\n")
        f.write("These are user messages matching the query used to generate this report.\n\n")
        for m in mentions:
            excerpt = _first_paragraph(m.text, 240)
            f.write(
                f"- {m.create_time_iso or ''} | {m.title} | conv `{m.conversation_id}` | msg `{m.message_rowid}`\n"
            )
            f.write(f"  - {excerpt}\n")

    by_conv: Dict[str, List[Mention]] = {}
    for m in mentions:
        by_conv.setdefault(m.conversation_id, []).append(m)

    conv_rows: List[Tuple[Optional[str], str, str, int, str]] = []
    for cid, ms in by_conv.items():
        ms_sorted = sorted(ms, key=lambda x: (x.create_time_iso or "9999", x.message_rowid))
        earliest = ms_sorted[0].create_time_iso
        title = ms_sorted[0].title
        best = max(ms_sorted, key=lambda x: _score_excerpt(x.text))
        best_excerpt = _first_paragraph(best.text, 320)
        conv_rows.append((earliest, cid, title, len(ms_sorted), best_excerpt))

    conv_rows.sort(key=lambda x: (x[0] or "9999", x[2]))

    with grouped_md_path.open("w", encoding="utf-8") as f:
        f.write("# Book-writing mentions (grouped by conversation)\n\n")
        f.write("Each entry shows earliest match date, conversation title/id, count, and a representative excerpt.\n\n")
        for earliest, cid, title, count, excerpt in conv_rows:
            f.write(f"- {earliest or ''} | hits={count} | {title} | conv `{cid}`\n")
            f.write(f"  - {excerpt}\n")

    return csv_path, md_path, grouped_md_path


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(
        description="Find user mentions about writing a book and export a message-level + conversation-level report."
    )
    p.add_argument("--db", default=default_db_path(), help="SQLite DB path")
    p.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help=f"FTS query (default: {DEFAULT_QUERY})",
    )
    p.add_argument(
        "--mode",
        choices=["like", "fts"],
        default="fts",
        help="Candidate selection mode: 'like' (default, heuristic) or 'fts' (use --query).",
    )
    p.add_argument("--out-dir", default=str(default_output_dir()), help="Output directory")
    p.add_argument("--csv", default="book_mentions.csv", help="CSV filename")
    p.add_argument("--md", default="book_mentions.md", help="Message-level Markdown filename")
    p.add_argument(
        "--grouped-md",
        default="book_mentions_by_conversation.md",
        help="Conversation-level Markdown filename",
    )
    args = p.parse_args(argv)

    mentions = find_mentions(Path(args.db), args.query, mode=args.mode)
    if not mentions:
        print("no matches", file=sys.stderr)
        return 1

    csv_path, md_path, grouped_md_path = write_outputs(
        mentions,
        out_dir=Path(args.out_dir),
        csv_name=args.csv,
        md_name=args.md,
        grouped_md_name=args.grouped_md,
    )
    print(f"found {len(mentions)} matching user messages", file=sys.stderr)
    print(f"wrote {csv_path}", file=sys.stderr)
    print(f"wrote {md_path}", file=sys.stderr)
    print(f"wrote {grouped_md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
