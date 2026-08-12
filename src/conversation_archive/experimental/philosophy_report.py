#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..chatgpt.paths import default_db_path, default_output_dir

@dataclass(frozen=True)
class Message:
    id: int
    conversation_id: str
    title: str
    seq: int
    role: str
    time_iso: Optional[str]
    text: str


@dataclass(frozen=True)
class ConversationSummary:
    conversation_id: str
    title: str
    first_time_iso: Optional[str]
    last_time_iso: Optional[str]
    hit_count: int
    score: float
    top_messages: List[Message]
    author_refs: List[str]


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, n: int) -> str:
    t = _clean_ws(text)
    if len(t) <= n:
        return t
    return t[: n - 1].rstrip() + "…"


def _looks_like_code_or_log(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    low = t.lower()
    if low.startswith("```") or "diff --git" in low:
        return True
    if low.startswith("import ") or low.startswith("export const "):
        return True
    if "traceback (most recent call last)" in low or "exception" in low and "line " in low:
        return True
    if re.search(r"\b(def|class|public|private|protected|return|function)\b", low) and "\n" in low:
        return True
    if "=>" in low or "end" in low and re.search(r"@[a-z_]+", low):
        return True
    punct = sum(t.count(ch) for ch in "{}[]();<>=$`")
    letters = sum(c.isalpha() for c in t)
    if letters > 0 and punct / max(1, letters) > 0.25:
        return True
    return False


def _looks_like_transcript_or_web_paste(text: str) -> bool:
    t = text.strip().lower()
    if t.startswith("transcript") or t.startswith("skip to main content"):
        return True
    if "subscribers" in t and "views" in t and ("youtube" in t or "podcast" in t or "subscribe" in t):
        return True
    if "subscribe" in t and "views" in t and "shares" in t:
        return True
    if t.startswith("notebook export"):
        return True
    ts_hits = len(re.findall(r"\b\d{1,2}:\d{2}\b", t[:1200]))
    return ts_hits >= 8


def _looks_like_status_update(text: str) -> bool:
    low = text.strip().lower()
    if ":white_check_mark:" in low:
        return True
    if low.count("✅") >= 2 or low.count("☑") >= 2:
        return True
    # Heuristic: many short lines with status verbs.
    lines = [ln.strip().lower() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 6:
        hits = sum(
            1
            for ln in lines[:24]
            if ln.startswith(("working on", "worked on", "blocked", "done", "today", "yesterday"))
        )
        if hits >= 3:
            return True
    return False


def _redact_basic(text: str) -> str:
    # Light-touch redaction: emails + phone-like digit runs.
    t = text
    t = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[redacted-email]", t, flags=re.I)
    t = re.sub(
        r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b",
        "[redacted-phone]",
        t,
    )
    return t


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


INSIGHT_PAT = re.compile(
    r"\b(i (just )?realized|i've realized|i learned|lesson|takeaway|"
    r"i decided|i'm deciding|i changed my mind|key insight)\b",
    re.I,
)

REFLECTION_PAT = re.compile(
    r"\b(the key is|i think the key is|my principle|my values|i value|rule of thumb|in hindsight)\b",
    re.I,
)

USER_RECAP_PAT = re.compile(r"^\s*(here('|’)s|heres)\s+(a\s+)?(summary|recap)\b", re.I)


THEME_KEYWORDS: Dict[str, Sequence[str]] = {
    "Meaning / Purpose": [
        "meaning of life",
        "life purpose",
        "purpose",
        "mission",
        "calling",
        "fulfillment",
        "existential",
        "existentialism",
        "nihilism",
        "mortality",
        "death",
        "suffering",
        "happiness",
        "eudaimonia",
    ],
    "Values / Principles / Ethics": [
        "my values",
        "core values",
        "principle",
        "principles",
        "ethics",
        "moral",
        "virtue",
        "good life",
        "integrity",
        "character",
        "duty",
        "responsibility",
    ],
    "Human Needs / Psychology": [
        "human needs",
        "psychological needs",
        "maslow",
        "self-determination",
        "self determination",
        "attachment",
        "attachment theory",
        "attachment style",
        "trauma",
        "anxiety",
        "depression",
        "self esteem",
        "self-worth",
        "therapy",
        "ego",
        "subconscious",
    ],
    "Identity / Authenticity": [
        "identity",
        "authentic",
        "authenticity",
        "self respect",
        "self-respect",
        "boundaries",
        "shame",
        "confidence",
        "masculine",
        "feminine",
    ],
    "Love / Connection": [
        "connection",
        "loneliness",
        "belonging",
        "community",
        "love",
    ],
    "Work / Ambition": [
        "ambition",
        "career",
        "success",
        "meaningful work",
        "purposeful work",
        "discipline",
        "motivation",
        "habits",
        "focus",
    ],
    "Spirituality / Religion": [
        "spiritual",
        "god",
        "religion",
        "faith",
        "buddh",
        "meditation",
        "prayer",
        "church",
        "christian",
        "jewish",
        "muslim",
        "hindu",
        "tao",
    ],
}


AUTHOR_TERMS: Sequence[str] = [
    # Philosophy / existential
    "Nietzsche",
    "Kierkegaard",
    "Camus",
    "Sartre",
    "Heidegger",
    "Schopenhauer",
    "Spinoza",
    "Aristotle",
    "Plato",
    "Stoicism",
    "Marcus Aurelius",
    "Seneca",
    "Epictetus",
    # Psychology / meaning
    "Viktor Frankl",
    "Man's Search for Meaning",
    "Carl Jung",
    "Jung",
    "Joseph Campbell",
    "Maslow",
    "Haidt",
    # Contemporary
    "Jordan Peterson",
    "Sam Harris",
    "Naval",
]


ANCHOR_SUBSTRINGS: Sequence[str] = [
    # Phrases/terms that are unlikely to be “incidental” in coding/business chats.
    "meaning of life",
    "life purpose",
    "core values",
    "my values",
    "worldview",
    "philosophy",
    "existential",
    "nihilism",
    "stoic",
    "stoicism",
    "virtue",
    "ethics",
    "morality",
    "good life",
    "eudaimonia",
    "mortality",
    "spiritual",
    "religion",
    "faith",
]

CORE_THEMES: Set[str] = {
    "Meaning / Purpose",
    "Values / Principles / Ethics",
    "Human Needs / Psychology",
    "Identity / Authenticity",
    "Spirituality / Religion",
}

NOISY_TITLE_PAT = re.compile(
    r"\b(resume|application|interview|cover letter|wedding speech|speech|job|offer|equity|agreement|invoice|dun & bradstreet|d&b)\b",
    re.I,
)

NOISY_TEXT_PAT = re.compile(
    r"\b(dun\s*&\s*bradstreet|d-u-n-s|business information report|daily standup|:white_check_mark:)\b",
    re.I,
)


def _fts_or(terms: Sequence[str]) -> str:
    parts: List[str] = []
    for t in terms:
        t = t.strip()
        if not t:
            continue
        if " " in t or "-" in t or "'" in t:
            parts.append(f"\"{t}\"")
        else:
            parts.append(t)
    return " OR ".join(parts)


def _candidate_conversations(conn: sqlite3.Connection, queries: Sequence[Tuple[str, str]]) -> Tuple[Dict[str, int], List[str]]:
    cur = conn.cursor()
    hits_per_conv: Dict[str, int] = {}
    used_queries: List[str] = []
    for name, q in queries:
        used_queries.append(f"{name}: {q}")
        rows = cur.execute(
            """
            SELECT m.conversation_id, COUNT(*) AS n
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            WHERE messages_fts MATCH ?
            GROUP BY m.conversation_id
            """,
            (q,),
        ).fetchall()
        for r in rows:
            cid = r["conversation_id"]
            hits_per_conv[cid] = hits_per_conv.get(cid, 0) + int(r["n"] or 0)
    return hits_per_conv, used_queries


def _fetch_conversation_messages(
    cur: sqlite3.Cursor, conversation_id: str
) -> Tuple[str, Optional[str], Optional[str], List[Message]]:
    conv = cur.execute(
        "SELECT title, create_time_iso, update_time_iso FROM conversations WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    title = (conv["title"] if conv else "") or ""
    create_iso = (conv["create_time_iso"] if conv else None) or None
    update_iso = (conv["update_time_iso"] if conv else None) or None
    rows = cur.execute(
        """
        SELECT id, conversation_id, seq, role, create_time_iso, text
        FROM messages
        WHERE conversation_id=?
        ORDER BY seq ASC
        """,
        (conversation_id,),
    ).fetchall()
    msgs: List[Message] = []
    for r in rows:
        msgs.append(
            Message(
                id=int(r["id"]),
                conversation_id=r["conversation_id"],
                title=title,
                seq=int(r["seq"] or 0),
                role=(r["role"] or "unknown").lower(),
                time_iso=r["create_time_iso"] or None,
                text=r["text"] or "",
            )
        )
    return title, create_iso, update_iso, msgs


def _theme_hits(text: str) -> Set[str]:
    low = text.lower()
    out: Set[str] = set()
    for theme, kws in THEME_KEYWORDS.items():
        for kw in kws:
            if kw in low:
                out.add(theme)
                break
    return out


def _author_hits(text: str) -> List[str]:
    out: List[str] = []
    for a in AUTHOR_TERMS:
        if re.search(rf"\b{re.escape(a)}\b", text, flags=re.I):
            out.append(a)
    # stable and de-duped
    seen: Set[str] = set()
    uniq: List[str] = []
    for a in out:
        key = a.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(a)
    return uniq


def _score_message(msg: Message) -> Tuple[float, Set[str], List[str]]:
    text = msg.text or ""
    t = text.strip()
    if not t:
        return (-10.0, set(), [])
    if _looks_like_code_or_log(t) or _looks_like_transcript_or_web_paste(t):
        return (-5.0, set(), [])
    if _looks_like_status_update(t):
        return (-5.0, set(), [])
    if NOISY_TEXT_PAT.search(t):
        return (-5.0, set(), [])

    themes = _theme_hits(t)
    authors = _author_hits(t)

    # Gate: require some evidence this is “philosophy-ish”, not incidental keyword overlap.
    low = t.lower()
    has_anchor = any(a in low for a in ANCHOR_SUBSTRINGS)
    if not themes and not authors and not has_anchor:
        return (-2.0, set(), [])

    # Avoid obvious non-philosophy operational threads by title unless strongly anchored.
    if msg.title and NOISY_TITLE_PAT.search(msg.title) and not has_anchor and not authors:
        return (-2.0, themes, authors)

    # Drop short or likely-incidental messages unless they contain explicit insight markers.
    if len(t) < 120 and not INSIGHT_PAT.search(t) and not REFLECTION_PAT.search(t) and not has_anchor and not authors:
        return (-1.0, themes, authors)

    # Don't let generic “insight” keywords pull in irrelevant short messages.
    if INSIGHT_PAT.search(t) and not themes and not has_anchor and not authors:
        return (-1.0, themes, authors)

    score = 0.0
    if msg.role == "user":
        score += 2.0
    # Favor substantive messages but cap.
    score += min(3.0, len(t) / 900.0)
    score += 0.7 * len(themes)
    score += 1.1 * len(authors)
    if INSIGHT_PAT.search(t):
        score += 2.0
    if REFLECTION_PAT.search(t):
        score += 1.0
    if USER_RECAP_PAT.search(t):
        score -= 0.5  # still useful, but often repeats
    if len(t) > 12000:
        score -= 1.0

    # Penalize “too generic” ambition-only matches unless anchored.
    if themes == {"Work / Ambition"} and not has_anchor and not authors and not INSIGHT_PAT.search(t):
        score -= 2.0
    # Penalize “love/connection” matches that are likely incidental unless anchored.
    if themes == {"Love / Connection"} and len(t) < 260 and not has_anchor and not authors and not INSIGHT_PAT.search(t):
        score -= 1.5
    return (score, themes, authors)


def _extract_quote_candidates(text: str) -> List[str]:
    # Very lightweight: pull 1–3 sentences containing “I …” / “my …” / “the key …”
    t = _clean_ws(text)
    if not t:
        return []
    parts = re.split(r"(?<=[.!?])\s+", t)
    out: List[str] = []
    for p in parts:
        p = p.strip()
        if len(p) < 40 or len(p) > 340:
            continue
        if re.search(
            r"\b(i (think|feel|believe|value|need|want|learned|realized)|my (values|principles)|the key is|in hindsight)\b",
            p,
            re.I,
        ):
            out.append(p)
        if len(out) >= 3:
            break
    return out


def build_report(
    db_path: Path,
    *,
    max_conversations_with_excerpts: int,
    excerpts_per_conversation: int,
    max_timeline_items: int,
    roles: Optional[Set[str]],
    out_path: Path,
) -> str:
    conn = _connect(db_path)
    cur = conn.cursor()

    # Broad coverage queries, split to keep each query string reasonable.
    topic_queries: List[Tuple[str, str]] = [
        (
            "meaning_values",
            "("
            + _fts_or(
                [
                    "meaning of life",
                    "life purpose",
                    "worldview",
                    "life philosophy",
                    "philosophy",
                    "my values",
                    "core values",
                    "ethics",
                    "morality",
                    "virtue",
                    "good life",
                    "eudaimonia",
                    "my principles",
                ]
            )
            + ")",
        ),
        (
            "psych_needs",
            "("
            + _fts_or(
                [
                    "human needs",
                    "psychological needs",
                    "Maslow",
                    "self-determination",
                    "attachment theory",
                    "attachment style",
                ]
            )
            + ")",
        ),
        (
            "existential_spiritual",
            "("
            + _fts_or(
                [
                    "existential",
                    "existentialism",
                    "nihilism",
                    "mortality",
                    "death",
                    "spiritual",
                    "god",
                    "faith",
                    "religion",
                    "buddhism",
                    "stoicism",
                    "taoism",
                    "meditation",
                    "prayer",
                ]
            )
            + ")",
        ),
        ("authors", "(" + _fts_or(AUTHOR_TERMS) + ")"),
    ]

    hits_per_conv, used_queries = _candidate_conversations(conn, topic_queries)
    candidate_convs = sorted(hits_per_conv.keys())

    conv_summaries: List[ConversationSummary] = []
    theme_counts: Dict[str, int] = {k: 0 for k in THEME_KEYWORDS.keys()}
    author_counts: Dict[str, int] = {}

    timeline_candidates: List[Tuple[Optional[datetime], Message, float, List[str]]] = []

    total_messages_scanned = 0
    messages_considered = 0

    for cid in candidate_convs:
        title, create_iso, update_iso, msgs = _fetch_conversation_messages(cur, cid)
        total_messages_scanned += len(msgs)

        per_msg_scores: List[Tuple[float, Message, Set[str], List[str]]] = []
        conv_author_refs: Set[str] = set()

        for m in msgs:
            if roles is not None and m.role not in roles:
                continue
            score, themes, authors = _score_message(m)
            if score <= 0:
                continue
            messages_considered += 1
            for th in themes:
                theme_counts[th] = theme_counts.get(th, 0) + 1
            for a in authors:
                conv_author_refs.add(a)
                author_counts[a] = author_counts.get(a, 0) + 1
            per_msg_scores.append((score, m, themes, authors))

            # Timeline candidates: require insight marker AND a “core” theme or an anchor/author.
            if INSIGHT_PAT.search(m.text or "") and (has_anchor := any(a in (m.text or "").lower() for a in ANCHOR_SUBSTRINGS) or bool(authors) or bool(themes & CORE_THEMES)):
                timeline_candidates.append((_parse_iso(m.time_iso), m, score, authors))

        if not per_msg_scores:
            continue

        per_msg_scores.sort(key=lambda x: x[0], reverse=True)
        top_msgs = [t[1] for t in per_msg_scores[:excerpts_per_conversation]]

        # Conversation score: sum of best few messages.
        conv_score = sum(s for s, _, _, _ in per_msg_scores[: min(6, len(per_msg_scores))])

        times = [t for t in (_parse_iso(m.time_iso) for m in msgs) if t is not None]
        first = min(times).isoformat() if times else create_iso
        last = max(times).isoformat() if times else update_iso

        conv_summaries.append(
            ConversationSummary(
                conversation_id=cid,
                title=title,
                first_time_iso=first,
                last_time_iso=last,
                hit_count=hits_per_conv.get(cid, 0),
                score=conv_score,
                top_messages=top_msgs,
                author_refs=sorted(conv_author_refs, key=lambda x: x.lower()),
            )
        )

    conv_summaries.sort(key=lambda c: c.score, reverse=True)
    timeline_candidates.sort(key=lambda x: (x[0] or datetime.max.replace(tzinfo=timezone.utc), -x[2]))

    # Build “crucial insights” timeline items with light de-dupe (by message id).
    seen_ids: Set[int] = set()
    timeline_items: List[Tuple[Optional[datetime], Message, float, List[str], List[str]]] = []
    for dt, m, sc, authors in timeline_candidates:
        if m.id in seen_ids:
            continue
        seen_ids.add(m.id)
        quotes = _extract_quote_candidates(m.text or "")
        timeline_items.append((dt, m, sc, authors, quotes))
        if len(timeline_items) >= max_timeline_items:
            break

    # References index: order by count desc.
    author_index = sorted(author_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))

    # Themes: order by count desc.
    theme_index = sorted(theme_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    # Markdown
    parts: List[str] = []
    parts.append("# Life philosophy / values / meaning — extracted report\n")
    parts.append(f"- db: `{db_path}`")
    parts.append(f"- generated: `{datetime.now(timezone.utc).isoformat()}`")
    parts.append(f"- candidate conversations: `{len(candidate_convs)}`")
    parts.append(f"- conversations with excerpts: `{len(conv_summaries)}`")
    parts.append(f"- total messages scanned in candidate conversations: `{total_messages_scanned}`")
    parts.append(f"- messages considered high-signal (heuristic): `{messages_considered}`\n")

    parts.append("## Queries used\n")
    for q in used_queries:
        parts.append(f"- `{q}`")
    parts.append("")

    parts.append("## Theme frequency (heuristic)\n")
    for theme, n in theme_index:
        parts.append(f"- {theme}: `{n}`")
    parts.append("")

    parts.append("## Authors / philosophies referenced (heuristic)\n")
    if author_index:
        for a, n in author_index:
            parts.append(f"- {a}: `{n}`")
    else:
        parts.append("- (none found from the author list; you can expand AUTHOR_TERMS)")
    parts.append("")

    parts.append("## Timeline of “crucial insight” candidates (heuristic)\n")
    for dt, m, sc, authors, quotes in timeline_items:
        when = dt.isoformat() if dt else (m.time_iso or "")
        parts.append(f"### {when}\n")
        parts.append(f"- title: {m.title}")
        parts.append(f"- conv: `{m.conversation_id}`")
        parts.append(f"- msg_id: `{m.id}` seq `{m.seq}` role `{m.role}` score `{sc:.2f}`")
        if authors:
            parts.append(f"- refs: {', '.join(f'`{a}`' for a in authors)}")
        if quotes:
            parts.append("- excerpts:")
            for q in quotes:
                q = _redact_basic(q)
                parts.append(f"  - “{_truncate(q, 280)}”")
        else:
            blob = _redact_basic(_truncate(m.text, 360))
            parts.append(f"- snippet: “{blob}”")
        parts.append("")

    parts.append("## Top conversations (with excerpts)\n")
    top = conv_summaries[:max_conversations_with_excerpts]
    for c in top:
        parts.append(f"### {c.title}\n")
        parts.append(f"- conv: `{c.conversation_id}`")
        parts.append(f"- time: `{c.first_time_iso or ''}` → `{c.last_time_iso or ''}`")
        parts.append(f"- fts_hit_count: `{c.hit_count}` score: `{c.score:.2f}`")
        if c.author_refs:
            parts.append(f"- refs: {', '.join(f'`{a}`' for a in c.author_refs)}")
        parts.append("- excerpts:")
        for m in c.top_messages:
            t = _redact_basic(_truncate(m.text, 320))
            parts.append(f"  - ({m.time_iso or ''}) `{m.role}` msg `{m.id}`: “{t}”")
        parts.append("")

    # Appendix: all candidates
    parts.append("## Appendix: all candidate conversations (metadata only)\n")
    for c in conv_summaries:
        parts.append(
            f"- `{c.first_time_iso or ''}` hits `{c.hit_count}` score `{c.score:.2f}` conv `{c.conversation_id}` title: {c.title}"
        )
    parts.append("")

    out = "\n".join(parts).rstrip() + "\n"
    out_path.write_text(out, encoding="utf-8")
    return out


def _parse_roles(value: str) -> Set[str]:
    return {r.strip().lower() for r in value.split(",") if r.strip()}


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(
        description="Extract and summarize high-signal life philosophy/values/meaning content from the ChatGPT export DB."
    )
    p.add_argument("--db", default=default_db_path(), help="SQLite DB path")
    p.add_argument(
        "--roles",
        default="user",
        help="Comma-separated roles to consider (default: user)",
    )
    p.add_argument(
        "--max-conversations-with-excerpts",
        type=int,
        default=80,
        help="How many conversations to include with excerpts (default: 80)",
    )
    p.add_argument(
        "--excerpts-per-conversation",
        type=int,
        default=3,
        help="How many excerpts per conversation (default: 3)",
    )
    p.add_argument(
        "--max-timeline-items",
        type=int,
        default=120,
        help="How many timeline insight items to include (default: 120)",
    )
    p.add_argument(
        "--out",
        default=str(default_output_dir() / "life_philosophy_extracted_report.md"),
        help="Output markdown path",
    )
    args = p.parse_args(argv)
    roles = _parse_roles(args.roles) if args.roles else None

    build_report(
        Path(args.db),
        max_conversations_with_excerpts=args.max_conversations_with_excerpts,
        excerpts_per_conversation=args.excerpts_per_conversation,
        max_timeline_items=args.max_timeline_items,
        roles=roles,
        out_path=Path(args.out),
    )
    print(f"wrote: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
