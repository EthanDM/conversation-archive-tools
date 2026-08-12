#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from ..chatgpt.paths import default_db_path

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'-]{2,}")

# Keep this list short; we only want to remove obvious noise.
STOPWORDS = {
    "a",
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "down",
    "during",
    "each",
    "few",
    "for",
    "from",
    "further",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "hers",
    "herself",
    "him",
    "himself",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "itself",
    "just",
    "me",
    "more",
    "most",
    "my",
    "myself",
    "no",
    "nor",
    "not",
    "now",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "our",
    "ours",
    "ourselves",
    "out",
    "over",
    "own",
    "same",
    "she",
    "should",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "themselves",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "too",
    "under",
    "until",
    "up",
    "very",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "why",
    "will",
    "with",
    "you",
    "your",
    "yours",
    "yourself",
    "yourselves",
}

PROJECT_KEYWORDS = {
    "project",
    "launch",
    "roadmap",
    "milestone",
    "mvp",
    "spec",
    "feature",
    "bug",
    "fix",
    "client",
    "proposal",
    "plan",
    "sprint",
    "ticket",
    "task",
    "shipping",
    "deadline",
    "build",
    "implement",
    "design",
    "refactor",
    "release",
    "rollout",
    "epic",
    "jira",
    "clickup",
}

AREA_KEYWORDS = {
    "health",
    "fitness",
    "diet",
    "sleep",
    "money",
    "finance",
    "tax",
    "invest",
    "career",
    "relationship",
    "relationships",
    "family",
    "home",
    "legal",
    "insurance",
    "workout",
    "travel",
    "spiritual",
    "therapy",
    "caffeine",
}


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_title(title: str) -> str:
    title = (title or "").strip() or "Untitled"
    title = re.sub(r"[\\/:*?\"<>|]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title[:160]


def _safe_filename(text: str) -> str:
    text = _normalize_title(text)
    text = re.sub(r"[^\w\s\.-]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "untitled"
    return text


def _tokenize(text: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for tok in TOKEN_RE.findall(text.lower()):
        if tok in STOPWORDS:
            continue
        if tok.isdigit():
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _get_terms(
    cur: sqlite3.Cursor, conversation_id: str, limit: int
) -> List[str]:
    try:
        rows = cur.execute(
            """
            SELECT term
            FROM conversation_terms
            WHERE conversation_id = ?
            ORDER BY weight DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(r["term"]) for r in rows if r["term"]]


def _pick_topics(title: str, tags: Sequence[str], max_topics: int) -> List[str]:
    topics: List[str] = []
    for tok in _tokenize(title):
        topics.append(tok)
        if len(topics) >= max_topics:
            return topics
    for tag in tags:
        if tag not in topics:
            topics.append(tag)
            if len(topics) >= max_topics:
                break
    return topics


def _parse_iso_date(iso_str: Optional[str]) -> Optional[dt.datetime]:
    if not iso_str:
        return None
    try:
        return dt.datetime.fromisoformat(iso_str)
    except ValueError:
        return None


def _pick_para(
    title: str,
    tags: Sequence[str],
    updated_iso: Optional[str],
    archive_days: int,
) -> str:
    if archive_days > 0:
        updated_dt = _parse_iso_date(updated_iso)
        if updated_dt is not None:
            age_days = (dt.datetime.now(tz=updated_dt.tzinfo) - updated_dt).days
            if age_days >= archive_days:
                return "Archives"

    tokens = set(_tokenize(title))
    tokens.update(tags)
    if tokens & PROJECT_KEYWORDS:
        return "Projects"
    if tokens & AREA_KEYWORDS:
        return "Areas"
    return "Resources"


def _yaml_list(items: Sequence[str], indent: int = 2) -> str:
    if not items:
        return "[]"
    pad = " " * indent
    return "\n" + "\n".join(f"{pad}- {item}" for item in items)


def _yaml_quote(value: str) -> str:
    value = value.replace("\n", " ").strip()
    escaped = value.replace("\"", "\\\"")
    return f"\"{escaped}\""


def _render_frontmatter(
    *,
    title: str,
    date: str,
    created: Optional[str],
    updated: Optional[str],
    source: str,
    conversation_id: str,
    topics: Sequence[str],
    tags: Sequence[str],
    para: str,
) -> str:
    lines = [
        "---",
        f"title: {_yaml_quote(title)}",
        f"date: {date}",
        f"created: {created or ''}",
        f"updated: {updated or ''}",
        f"source: {source}",
        f"conversation_id: {conversation_id}",
        f"para: {para}",
        "topics:" + _yaml_list(topics),
        "tags:" + _yaml_list(tags),
        "---",
        "",
    ]
    return "\n".join(lines)


def _iter_messages(
    cur: sqlite3.Cursor,
    conversation_id: str,
    include_roles: Optional[set[str]],
) -> Iterable[sqlite3.Row]:
    rows = cur.execute(
        """
        SELECT role, create_time_iso, text
        FROM messages
        WHERE conversation_id = ?
        ORDER BY seq ASC
        """,
        (conversation_id,),
    )
    for row in rows:
        role = str(row["role"] or "")
        if include_roles and role not in include_roles:
            continue
        yield row


def export_kb(
    db_path: Path,
    out_dir: Path,
    *,
    max_tags: int,
    max_topics: int,
    archive_days: int,
    include_roles: Optional[set[str]],
    by_year: bool,
) -> int:
    conn = _connect(db_path)
    cur = conn.cursor()

    conv_rows = cur.execute(
        """
        SELECT conversation_id, title, create_time_iso, update_time_iso
        FROM conversations
        ORDER BY update_time DESC
        """
    ).fetchall()

    if not conv_rows:
        print("no conversations found")
        return 1

    for conv in conv_rows:
        conversation_id = str(conv["conversation_id"])
        title = _normalize_title(str(conv["title"] or "Untitled"))
        created_iso = conv["create_time_iso"]
        updated_iso = conv["update_time_iso"]
        date_iso = updated_iso or created_iso or ""
        date_only = date_iso[:10] if date_iso else ""

        tags = _get_terms(cur, conversation_id, max_tags)
        topics = _pick_topics(title, tags, max_topics)
        para = _pick_para(title, tags, updated_iso, archive_days)

        year = date_only[:4] if date_only else "unknown"
        dest_dir = out_dir / para
        if by_year:
            dest_dir = dest_dir / year
        dest_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{date_only} - {_safe_filename(title)} - {conversation_id}.md"
        path = dest_dir / filename

        frontmatter = _render_frontmatter(
            title=title,
            date=date_only,
            created=created_iso,
            updated=updated_iso,
            source="chatgpt",
            conversation_id=conversation_id,
            topics=topics,
            tags=tags,
            para=para,
        )

        with path.open("w", encoding="utf-8") as f:
            f.write(frontmatter)
            f.write(f"# {title}\n\n")
            for row in _iter_messages(cur, conversation_id, include_roles):
                role = str(row["role"] or "unknown")
                ts = row["create_time_iso"]
                ts_line = f" ({ts})" if ts else ""
                text = (row["text"] or "").strip()
                if not text:
                    continue
                f.write(f"## {role}{ts_line}\n\n")
                f.write(text + "\n\n")

    return 0


def _parse_roles(value: Optional[str]) -> Optional[set[str]]:
    if not value:
        return None
    roles = {r.strip() for r in value.split(",") if r.strip()}
    return roles or None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export ChatGPT SQLite index into PARA-organized Markdown."
    )
    parser.add_argument("--db", default=default_db_path())
    parser.add_argument("--out", default="knowledge_base")
    parser.add_argument("--max-tags", type=int, default=8)
    parser.add_argument("--max-topics", type=int, default=4)
    parser.add_argument("--archive-days", type=int, default=365)
    parser.add_argument(
        "--roles",
        default="user,assistant",
        help="Comma-separated roles to include (default: user,assistant). Use empty to include all.",
    )
    parser.add_argument(
        "--no-by-year",
        action="store_true",
        help="Do not create year subfolders.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    out_dir = Path(args.out)
    include_roles = _parse_roles(args.roles)
    by_year = not args.no_by_year

    if not db_path.exists():
        print(f"db not found: {db_path}")
        return 1

    return export_kb(
        db_path,
        out_dir,
        max_tags=args.max_tags,
        max_topics=args.max_topics,
        archive_days=args.archive_days,
        include_roles=include_roles,
        by_year=by_year,
    )


if __name__ == "__main__":
    raise SystemExit(main())
