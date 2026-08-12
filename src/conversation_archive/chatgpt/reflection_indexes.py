#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .paths import default_db_path

TOPIC_RULES: list[dict[str, object]] = [
    {
        "label": "Coding & debugging",
        "sensitive": 0,
        "terms": [
            "code",
            "bug",
            "typescript",
            "javascript",
            "python",
            "react",
            "sql",
            "github",
            "pull request",
            "deploy",
            "api",
            "unit test",
            "integration test",
            "refactor",
            "codex",
        ],
    },
    {
        "label": "Business & clients",
        "sensitive": 0,
        "terms": [
            "client",
            "proposal",
            "pricing",
            "contract",
            "invoice",
            "scope",
            "startup",
            "sales",
            "business",
            "customer",
            "strategy",
            "consulting",
        ],
    },
    {
        "label": "Writing & ideas",
        "sensitive": 0,
        "terms": [
            "essay",
            "book",
            "chapter",
            "draft",
            "outline",
            "writing",
            "voice",
            "thesis",
            "argument",
            "blog",
            "edit",
            "copy",
        ],
    },
    {
        "label": "Life admin",
        "sensitive": 0,
        "terms": [
            "email",
            "calendar",
            "schedule",
            "appointment",
            "todo",
            "errand",
            "budget",
            "tax",
            "insurance",
            "home",
            "move",
            "organize",
        ],
    },
    {
        "label": "Travel & planning",
        "sensitive": 0,
        "terms": [
            "travel",
            "trip",
            "flight",
            "hotel",
            "itinerary",
            "switzerland",
            "hike",
            "route",
            "restaurant",
            "packing",
            "lodging",
        ],
    },
    {
        "label": "Learning & research",
        "sensitive": 0,
        "terms": [
            "explain",
            "learn",
            "research",
            "summary",
            "compare",
            "history",
            "philosophy",
            "science",
            "framework",
            "analysis",
            "understand",
        ],
    },
    {
        "label": "Health & body",
        "sensitive": 1,
        "terms": [
            "health",
            "doctor",
            "symptom",
            "skin",
            "rash",
            "pain",
            "sleep",
            "medicine",
            "therapy",
            "caffeine",
            "workout",
            "diet",
        ],
    },
    {
        "label": "Relationships & emotions",
        "sensitive": 1,
        "terms": [
            "relationship",
            "dating",
            "lonely",
            "loneliness",
            "anxious",
            "anxiety",
            "sad",
            "feel",
            "feelings",
            "friend",
            "family",
            "breakup",
        ],
    },
    {
        "label": "Finance & investing",
        "sensitive": 1,
        "terms": [
            "money",
            "finance",
            "invest",
            "stock",
            "portfolio",
            "mortgage",
            "loan",
            "salary",
            "revenue",
            "expense",
            "bank",
        ],
    },
]

LENSES: list[dict[str, str]] = [
    {
        "name": "Delegation",
        "query": "plan OR draft OR summarize OR automate OR checklist OR generate",
        "description": "Work where ChatGPT took a task off your plate.",
    },
    {
        "name": "Discernment",
        "query": "review OR critique OR evaluate OR compare OR better OR tradeoff",
        "description": "Conversations where you used ChatGPT as a second-pass evaluator.",
    },
    {
        "name": "Diligence",
        "query": "test OR verify OR source OR evidence OR risk OR edge",
        "description": "Use tied to checking correctness, quality, or evidence.",
    },
    {
        "name": "Original thinking",
        "query": "thesis OR idea OR strategy OR principle OR perspective OR argument",
        "description": "Chats that sharpened your own concepts instead of only producing output.",
    },
]

TOKEN_RE = re.compile(r"[a-z][a-z0-9_'-]{2,}")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _utc_iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _term_count(text: str, term: str, token_counts: Counter[str]) -> int:
    if " " in term:
        return text.count(term)
    return token_counts.get(term, 0)


def _evidence_terms(text: str, terms: list[str], token_counts: Counter[str]) -> tuple[int, list[str]]:
    matches: list[tuple[str, int]] = []
    for term in terms:
        count = _term_count(text, term, token_counts)
        if count:
            matches.append((term, count))
    matches.sort(key=lambda item: (-item[1], item[0]))
    return sum(count for _, count in matches), [term for term, _ in matches[:6]]


def _create_tables(cur: sqlite3.Cursor) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_metrics (
          conversation_id TEXT PRIMARY KEY,
          title TEXT,
          create_time REAL,
          update_time REAL,
          create_time_iso TEXT,
          update_time_iso TEXT,
          first_message_time REAL,
          last_message_time REAL,
          user_message_count INTEGER NOT NULL,
          assistant_message_count INTEGER NOT NULL,
          tool_message_count INTEGER NOT NULL,
          message_count INTEGER NOT NULL,
          text_chars INTEGER NOT NULL,
          code_message_count INTEGER NOT NULL,
          multimodal_message_count INTEGER NOT NULL,
          active_day_count INTEGER NOT NULL,
          duration_minutes REAL NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_metrics_create_time ON conversation_metrics(create_time)"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_topics (
          conversation_id TEXT NOT NULL,
          topic TEXT NOT NULL,
          score REAL NOT NULL,
          evidence_terms TEXT NOT NULL,
          is_sensitive INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (conversation_id, topic)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_topics_topic ON conversation_topics(topic, score DESC)"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reflection_summaries (
          range_key TEXT PRIMARY KEY,
          summary TEXT NOT NULL,
          bullets_json TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          source_kind TEXT NOT NULL,
          model TEXT,
          generated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_lenses (
          name TEXT PRIMARY KEY,
          query TEXT NOT NULL,
          description TEXT NOT NULL
        )
        """
    )


def _reset_tables(cur: sqlite3.Cursor) -> None:
    cur.execute("DELETE FROM conversation_metrics")
    cur.execute("DELETE FROM conversation_topics")
    cur.execute("DELETE FROM reflection_summaries")
    cur.execute("DELETE FROM dashboard_lenses")


def _build_metrics_and_topics(conn: sqlite3.Connection) -> tuple[int, int]:
    cur = conn.cursor()
    convs = cur.execute(
        """
        SELECT conversation_id, title, create_time, update_time, create_time_iso, update_time_iso
        FROM conversations
        ORDER BY create_time
        """
    ).fetchall()

    metrics_rows: list[tuple[object, ...]] = []
    topic_rows: list[tuple[object, ...]] = []

    for conv in convs:
        messages = cur.execute(
            """
            SELECT role, create_time, create_time_iso, content_type, text
            FROM messages
            WHERE conversation_id = ?
            ORDER BY seq
            """,
            (conv["conversation_id"],),
        ).fetchall()

        role_counts = Counter((m["role"] or "") for m in messages)
        content_counts = Counter((m["content_type"] or "") for m in messages)
        timestamps = [float(m["create_time"]) for m in messages if m["create_time"] is not None]
        days = {str(m["create_time_iso"] or "")[:10] for m in messages if m["create_time_iso"]}
        text_parts = [conv["title"] or ""]
        text_chars = 0
        for message in messages:
            text = message["text"] or ""
            text_chars += len(text)
            if message["role"] == "user":
                text_parts.append(text)

        first_time = min(timestamps) if timestamps else conv["create_time"]
        last_time = max(timestamps) if timestamps else conv["update_time"]
        duration_minutes = 0.0
        if first_time is not None and last_time is not None:
            duration_minutes = max(0.0, (float(last_time) - float(first_time)) / 60.0)

        metrics_rows.append(
            (
                conv["conversation_id"],
                conv["title"],
                conv["create_time"],
                conv["update_time"],
                conv["create_time_iso"],
                conv["update_time_iso"],
                first_time,
                last_time,
                role_counts.get("user", 0),
                role_counts.get("assistant", 0),
                role_counts.get("tool", 0),
                len(messages),
                text_chars,
                content_counts.get("code", 0),
                content_counts.get("multimodal_text", 0),
                len(days),
                duration_minutes,
            )
        )

        normalized_text = "\n".join(text_parts).lower()
        tokens = TOKEN_RE.findall(normalized_text)
        token_counts = Counter(tokens)
        token_count = max(1, len(tokens))
        scores: list[tuple[str, float, list[str], int]] = []
        for rule in TOPIC_RULES:
            terms = list(rule["terms"])  # type: ignore[arg-type]
            raw_count, evidence = _evidence_terms(normalized_text, terms, token_counts)
            if raw_count == 0:
                continue
            score = min(1.0, raw_count / max(12.0, token_count / 80.0))
            if score < 0.18:
                continue
            scores.append((str(rule["label"]), score, evidence, int(rule["sensitive"])))

        scores.sort(key=lambda item: (-item[1], item[0]))
        for label, score, evidence, is_sensitive in scores[:4]:
            topic_rows.append(
                (
                    conv["conversation_id"],
                    label,
                    score,
                    json.dumps(evidence),
                    is_sensitive,
                )
            )

    cur.executemany(
        """
        INSERT OR REPLACE INTO conversation_metrics (
          conversation_id, title, create_time, update_time, create_time_iso, update_time_iso,
          first_message_time, last_message_time, user_message_count, assistant_message_count,
          tool_message_count, message_count, text_chars, code_message_count,
          multimodal_message_count, active_day_count, duration_minutes
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        metrics_rows,
    )
    cur.executemany(
        """
        INSERT OR REPLACE INTO conversation_topics (
          conversation_id, topic, score, evidence_terms, is_sensitive
        )
        VALUES (?,?,?,?,?)
        """,
        topic_rows,
    )
    return len(metrics_rows), len(topic_rows)


def _range_sql(range_key: str) -> str:
    if range_key == "all":
        return "1=1"
    months = {"12m": 12, "6m": 6, "3m": 3, "1m": 1}[range_key]
    return (
        "create_time >= (SELECT COALESCE(MAX(create_time), 0) FROM conversation_metrics) "
        f"- {months * 31 * 24 * 60 * 60}"
    )


def _build_reflection_for_range(conn: sqlite3.Connection, range_key: str) -> None:
    cur = conn.cursor()
    where_sql = _range_sql(range_key)
    overview = cur.execute(
        f"""
        SELECT
          COUNT(*) AS conversations,
          COALESCE(SUM(user_message_count), 0) AS user_messages,
          COALESCE(SUM(assistant_message_count), 0) AS assistant_messages,
          COALESCE(SUM(text_chars), 0) AS text_chars,
          MIN(create_time_iso) AS first_seen,
          MAX(update_time_iso) AS last_seen
        FROM conversation_metrics
        WHERE {where_sql}
        """
    ).fetchone()
    topics = cur.execute(
        f"""
        SELECT topic, COUNT(*) AS conversations, AVG(score) AS avg_score
        FROM conversation_topics t
        JOIN conversation_metrics m ON m.conversation_id = t.conversation_id
        WHERE {where_sql}
        GROUP BY topic
        ORDER BY conversations DESC, avg_score DESC
        LIMIT 5
        """
    ).fetchall()
    examples = cur.execute(
        f"""
        SELECT conversation_id, title, create_time_iso, user_message_count + assistant_message_count AS messages
        FROM conversation_metrics
        WHERE {where_sql}
        ORDER BY messages DESC, update_time DESC
        LIMIT 5
        """
    ).fetchall()

    topic_names = [row["topic"] for row in topics]
    if overview["conversations"] == 0:
        summary = "No conversations are available for this range."
        bullets = ["Try a broader date range.", "The raw export remains unchanged."]
    else:
        range_label = "all time" if range_key == "all" else f"the last {range_key}"
        topic_phrase = ", ".join(topic_names[:3]) if topic_names else "mixed work"
        summary = (
            f"Across {range_label}, ChatGPT use is concentrated around {topic_phrase}. "
            f"The range includes {overview['conversations']} conversations and "
            f"{overview['user_messages']} user messages."
        )
        bullets = [
            f"Most visible themes: {', '.join(topic_names) if topic_names else 'No dominant topic detected'}.",
            f"Conversation span: {overview['first_seen'] or 'unknown'} to {overview['last_seen'] or 'unknown'}.",
            "Sensitive areas stay aggregated on the dashboard; transcripts remain available through explicit drilldown.",
        ]

    evidence = [
        {
            "conversation_id": row["conversation_id"],
            "title": row["title"],
            "create_time_iso": row["create_time_iso"],
            "messages": row["messages"],
        }
        for row in examples
    ]
    cur.execute(
        """
        INSERT OR REPLACE INTO reflection_summaries (
          range_key, summary, bullets_json, evidence_json, source_kind, model, generated_at
        )
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            range_key,
            summary,
            json.dumps(bullets),
            json.dumps(evidence),
            "deterministic",
            None,
            _utc_iso_now(),
        ),
    )


def _build_reflections(conn: sqlite3.Connection) -> None:
    for range_key in ["all", "12m", "6m", "3m", "1m"]:
        _build_reflection_for_range(conn, range_key)


def _build_lenses(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO dashboard_lenses(name, query, description) VALUES (?,?,?)",
        [(lens["name"], lens["query"], lens["description"]) for lens in LENSES],
    )


def build_indexes(db_path: Path, *, reset: bool) -> int:
    conn = _connect(db_path)
    cur = conn.cursor()
    _create_tables(cur)
    if reset:
        _reset_tables(cur)
    metrics_count, topic_count = _build_metrics_and_topics(conn)
    _build_reflections(conn)
    _build_lenses(conn)
    conn.commit()
    print(
        f"built reflection indexes for {metrics_count} conversations and {topic_count} topic links in {db_path}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build local reflection dashboard indexes for a ChatGPT export DB.")
    parser.add_argument("--db", default=default_db_path(), help="SQLite DB path.")
    parser.add_argument("--reset", action="store_true", help="Clear derived dashboard tables before rebuilding.")
    args = parser.parse_args(argv)
    return build_indexes(Path(args.db), reset=args.reset)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
