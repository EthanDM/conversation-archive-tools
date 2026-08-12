#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

from .paths import default_db_path

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'-]{2,}")

# Small, pragmatic stopword list (kept intentionally short).
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


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _iter_messages(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    cur = conn.cursor()
    return cur.execute(
        """
        SELECT conversation_id, text
        FROM messages
        WHERE text IS NOT NULL AND text != ''
        ORDER BY conversation_id, seq
        """
    )


def _tokenize(text: str) -> Iterable[str]:
    for tok in TOKEN_RE.findall(text.lower()):
        if tok in STOPWORDS:
            continue
        if tok.isdigit():
            continue
        yield tok


def build_terms(db_path: Path, *, max_terms: int, reset: bool) -> int:
    conn = _connect(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_terms (
          conversation_id TEXT NOT NULL,
          term TEXT NOT NULL,
          weight REAL NOT NULL,
          PRIMARY KEY (conversation_id, term)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_terms_term ON conversation_terms(term)"
    )

    if reset:
        cur.execute("DELETE FROM conversation_terms")
        conn.commit()

    # First pass: collect top terms per conversation, plus doc frequency.
    conv_top: dict[str, list[tuple[str, int]]] = {}
    df = Counter()

    current_conv: str | None = None
    counts: Counter[str] = Counter()

    def flush() -> None:
        nonlocal current_conv, counts
        if current_conv is None:
            return
        common = counts.most_common(max_terms)
        conv_top[current_conv] = common
        df.update({t for t, _ in common})
        counts = Counter()

    for row in _iter_messages(conn):
        conv = str(row["conversation_id"])
        if current_conv is None:
            current_conv = conv
        if conv != current_conv:
            flush()
            current_conv = conv
        text = row["text"] or ""
        counts.update(_tokenize(text))
    flush()

    if not conv_top:
        print("no conversations found", file=sys.stderr)
        return 1

    n_docs = len(conv_top)
    idf = {t: math.log((n_docs + 1.0) / (df_t + 1.0)) + 1.0 for t, df_t in df.items()}

    # Second pass: compute normalized TF-IDF weights and write to SQLite.
    rows_to_insert: list[tuple[str, str, float]] = []
    for conv_id, common in conv_top.items():
        weights: list[tuple[str, float]] = []
        for term, count in common:
            tf = 1.0 + math.log(max(1, int(count)))
            w = tf * idf.get(term, 0.0)
            if w > 0:
                weights.append((term, w))

        norm = math.sqrt(sum(w * w for _, w in weights)) or 1.0
        for term, w in weights:
            rows_to_insert.append((conv_id, term, w / norm))

    cur.execute("BEGIN")
    if reset:
        cur.execute("DELETE FROM conversation_terms")
    cur.executemany(
        "INSERT OR REPLACE INTO conversation_terms(conversation_id, term, weight) VALUES (?,?,?)",
        rows_to_insert,
    )
    conn.commit()

    print(
        f"built conversation_terms for {n_docs} conversations ({len(rows_to_insert)} rows) in {db_path}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Build a lightweight conversation-level TF-IDF term index for 'related conversations' browsing."
    )
    p.add_argument("--db", default=default_db_path(), help="SQLite DB path")
    p.add_argument(
        "--max-terms",
        type=int,
        default=200,
        help="Max terms stored per conversation (default: 200)",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="Rebuild from scratch (clears conversation_terms first)",
    )
    args = p.parse_args(argv)
    return build_terms(Path(args.db), max_terms=args.max_terms, reset=args.reset)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
