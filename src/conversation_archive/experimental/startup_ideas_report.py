#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..chatgpt.paths import default_db_path, default_output_dir

@dataclass
class Idea:
    domain: str
    first_time_iso: Optional[str]
    conversation_id: str
    title: str
    idea_excerpt: str
    liked: List[str]
    disliked: List[str]


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, n: int) -> str:
    text = _clean_ws(text)
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _looks_like_code_or_log(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    low = t.lower()
    if low.startswith("```") or "diff --git" in low:
        return True
    if low.startswith("import ") or low.startswith("export const "):
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
    if "subscribers" in t and "views" in t and "share" in t:
        return True
    if "youtube" in t and "subscribed" in t:
        return True
    if t.startswith("notebook export"):
        return True
    ts_hits = len(re.findall(r"\b\d{1,2}:\d{2}\b", t[:1000]))
    return ts_hits >= 8


def _looks_like_asset_pointer(text: str) -> bool:
    low = text.strip().lower()
    return "asset_pointer" in low and "file-service://" in low


def _extract_sentences(text: str) -> List[str]:
    # Lightweight sentence splitter; good enough for short “why” lines.
    t = text.replace("\n", " ").strip()
    parts = re.split(r"(?<=[\.\?\!])\s+", t)
    out: List[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        out.append(p)
    return out


IDEA_PATTERNS = [
    # Direct pitching / intent.
    re.compile(
        r"\b(i|i'm|im|i am|i want to|i’m|i would like to|i plan to|i think i want to)\b.{0,40}\b(build|create|start|launch|make)\b.{0,60}\b(startup|saas|business|product|marketplace|platform|tool|app|website)\b",
        re.I,
    ),
    re.compile(
        r"\b(i|i'm|im)\b.{0,40}\b(have|got)\b.{0,30}\b(a|an)\b.{0,10}\b(startup|business|product)\b.{0,15}\bidea\b",
        re.I,
    ),
    # “Explore” style: asking for ideas/opportunities.
    re.compile(r"\b(micro saas|micro-saas)\b.{0,40}\bideas?\b", re.I),
]


STARTUP_KEYWORDS = {
    "startup",
    "saas",
    "micro saas",
    "mrr",
    "revenue",
    "monetize",
    "pricing",
    "business idea",
    "marketplace",
    "side project",
    "product idea",
    "b2b",
}

TITLE_IGNORE_WIDE = re.compile(
    r"\b(error|bug|fix|solution|refactor|sdk|translation|pluralization|eslint|prisma|react|ios|android|build|qa|testing|date picker)\b",
    re.I,
)


def _contains_startup_keyword(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in STARTUP_KEYWORDS)


def _is_idea_statement(text: str, title: str, *, mode: str) -> bool:
    low = text.lower()
    title_low = (title or "").lower()
    if len(text) > 5000:
        return False
    if "facebook dating" in low and "bio" in low:
        return False
    if "create a typescript file" in low:
        return False
    if "privacy policy" in low:
        return False
    if "app icon" in low or "app store screenshots" in low:
        return False
    if low.startswith("meeting title:") or low.startswith("meeting participants:") or "transcript:" in low:
        return False
    if low.startswith("low level design:") or low.startswith("l0d:") or low.startswith("design doc:"):
        return False
    if low.startswith("lead eng said:") or low.startswith("lead engineer said:"):
        return False
    if "file-service://" in low:
        return False
    if "app store connect" in low or "in-app purchase" in low or "in app purchase" in low:
        return False
    if "subscription group" in low and "saas" not in low and "startup" not in low:
        return False
    if "service account" in low and "startup" not in low and "saas" not in low:
        return False
    if "about says" in low or "my about says" in low or "resume" in low or "portfolio" in low:
        return False
    if mode == "wide":
        # If the title looks like dev-triage, skip unless it is clearly about startup/business.
        if TITLE_IGNORE_WIDE.search(title_low) and not _contains_startup_keyword(title_low):
            return False
    else:
        # Strict mode: require at least one business/startup marker in title or message.
        if not _contains_startup_keyword(low) and not _contains_startup_keyword(title_low):
            return False
    for pat in IDEA_PATTERNS:
        if pat.search(text):
            return True
    return False


def _domain_of(title: str, text: str) -> str:
    blob = (title + " " + text).lower()
    if any(k in blob for k in ["dating", "alook", "hinge", "bumble", "tinder"]):
        return "Dating / Social"
    # Use word-boundary matches to avoid false positives (e.g., “sustaining” contains “ai”).
    if re.search(r"\b(llm|gpt|openai|chatgpt|ai)\b", blob):
        return "AI / LLM"
    if any(k in blob for k in ["micro saas", "saas"]):
        return "SaaS / Micro-SaaS"
    if any(k in blob for k in ["calendar", "booking", "crm", "salesforce", "pipeline", "lead"]):
        return "B2B Sales / CRM"
    if any(k in blob for k in ["health", "fitness", "steps", "sleep", "nutrition", "workout"]):
        return "Health / Fitness"
    if any(k in blob for k in ["fintech", "tax", "invoice", "budget", "invest", "portfolio"]):
        return "Finance"
    if any(k in blob for k in ["ios", "android", "react native", "app store"]):
        return "Mobile / App Dev"
    if any(k in blob for k in ["dashboard", "report", "analytics", "metrics"]):
        return "Analytics / Reporting"
    if any(k in blob for k in ["open source", "cli", "developer tool", "devtools"]):
        return "Developer Tools"
    return "Other / Unclear"


LIKED_PAT = re.compile(
    r"\b(i like|i love|excited|appeal|attractive|interesting|good because|great because|this is good|this seems good|seems promising|big opportunity|i think this could)\b",
    re.I,
)
DISLIKED_PAT = re.compile(
    r"\b(but|concern|downside|risk|hard|difficult|problem|issue|not sure|unsure|worry|tradeoff|too much|doesn't|won't)\b",
    re.I,
)


def _extract_likes_dislikes(user_texts: Sequence[str]) -> Tuple[List[str], List[str]]:
    liked: List[str] = []
    disliked: List[str] = []
    for t in user_texts:
        for s in _extract_sentences(t):
            if len(s) < 12:
                continue
            if len(s) > 240:
                continue
            if LIKED_PAT.search(s) and s not in liked:
                liked.append(s)
            if DISLIKED_PAT.search(s) and s not in disliked:
                disliked.append(s)
    # Prefer a couple of the most direct/short ones.
    liked = liked[:3]
    disliked = disliked[:3]
    return liked, disliked


def _candidate_conversations(conn: sqlite3.Connection, limit: int) -> List[str]:
    cur = conn.cursor()
    # Candidate selection using FTS (fast). We refine heavily in Python afterwards.
    fts_query = (
        "(startup OR saas OR \"micro saas\" OR marketplace OR \"business idea\" OR \"product idea\" "
        "OR mrr OR monetize OR revenue OR pricing OR \"side project\")"
    )
    rows = cur.execute(
        """
        SELECT m.conversation_id, MIN(m.create_time) AS first_time
        FROM messages_fts
        JOIN messages m ON m.id=messages_fts.rowid
        WHERE messages_fts MATCH ?
          AND m.role='user'
        GROUP BY m.conversation_id
        ORDER BY first_time ASC
        LIMIT ?
        """,
        (fts_query, limit),
    ).fetchall()
    return [r["conversation_id"] for r in rows if r["conversation_id"]]


def build_report(
    db_path: Path,
    *,
    limit_conversations: int,
    max_user_messages: int,
    out_path: Path,
    mode: str,
    exclude: Sequence[str],
) -> List[Idea]:
    conn = _connect(db_path)
    cur = conn.cursor()

    conv_ids = _candidate_conversations(conn, limit_conversations)
    ideas: List[Idea] = []

    for cid in conv_ids:
        conv = cur.execute(
            "SELECT conversation_id, title FROM conversations WHERE conversation_id=?",
            (cid,),
        ).fetchone()
        if conv is None:
            continue
        title = conv["title"] or ""
        if exclude:
            title_low = title.lower()
            if any(w in title_low for w in exclude):
                continue

        rows = cur.execute(
            """
            SELECT seq, role, create_time_iso, text
            FROM messages
            WHERE conversation_id=?
            ORDER BY seq ASC
            """,
            (cid,),
        ).fetchall()

        user_rows = [r for r in rows if (r["role"] or "").lower() == "user"]
        if not user_rows:
            continue

        user_texts: List[str] = []
        first_time_iso: Optional[str] = None
        idea_excerpt = ""

        for r in user_rows[:max_user_messages]:
            text = r["text"] or ""
            if (
                _looks_like_code_or_log(text)
                or _looks_like_transcript_or_web_paste(text)
                or _looks_like_asset_pointer(text)
            ):
                continue
            user_texts.append(text)
            if first_time_iso is None and r["create_time_iso"]:
                first_time_iso = r["create_time_iso"]
            if not idea_excerpt and _is_idea_statement(text, conv["title"] or "", mode=mode):
                idea_excerpt = _truncate(text, 280)

        if not idea_excerpt:
            # Skip convos that were matches but don't look like a distinct idea pitch.
            continue
        if exclude:
            ex_low = (title + " " + idea_excerpt).lower()
            if any(w in ex_low for w in exclude):
                continue

        liked, disliked = _extract_likes_dislikes(user_texts)
        domain = _domain_of(conv["title"] or "", idea_excerpt)

        ideas.append(
            Idea(
                domain=domain,
                first_time_iso=first_time_iso,
                conversation_id=cid,
                title=conv["title"] or "",
                idea_excerpt=idea_excerpt,
                liked=liked,
                disliked=disliked,
            )
        )

    # Write markdown grouped by domain
    by_domain: DefaultDict[str, List[Idea]] = DefaultDict(list)
    for i in ideas:
        by_domain[i.domain].append(i)
    for dom in by_domain:
        by_domain[dom].sort(key=lambda x: (x.first_time_iso or "9999", x.title))

    lines: List[str] = []
    lines.append("# Startup ideas (extracted)\n")
    lines.append(f"- db: `{db_path}`")
    lines.append(f"- ideas: `{len(ideas)}`")
    lines.append(f"- candidate_conversations_scanned: `{len(conv_ids)}`\n")
    lines.append(
        "This is heuristic (best-effort): it tries to find conversations where you pitch an idea (\"build/create/start X\").\n"
    )

    for dom in sorted(by_domain.keys()):
        lines.append(f"## {dom}\n")
        for i in by_domain[dom]:
            lines.append(
                f"- {i.first_time_iso or ''} | {i.title} | conv `{i.conversation_id}`"
            )
            lines.append(f"  - Idea: {i.idea_excerpt}")
            if i.liked:
                lines.append("  - Liked:")
                for s in i.liked:
                    lines.append(f"    - {s}")
            if i.disliked:
                lines.append("  - Concerns:")
                for s in i.disliked:
                    lines.append(f"    - {s}")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return ideas


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description="Extract startup ideas from a local ChatGPT index and group by domain.")
    p.add_argument("--db", default=default_db_path(), help="SQLite DB path")
    p.add_argument(
        "--limit-conversations",
        type=int,
        default=800,
        help="How many candidate conversations to scan (default: 800)",
    )
    p.add_argument(
        "--max-user-messages",
        type=int,
        default=120,
        help="Max user messages per conversation to inspect (default: 120)",
    )
    p.add_argument(
        "--out",
        default=str(default_output_dir() / "startup_ideas.md"),
        help="Output Markdown path",
    )
    p.add_argument(
        "--mode",
        choices=["strict", "wide"],
        default="wide",
        help="Strict requires startup keywords; wide tries to capture more idea pitches (default: wide).",
    )
    p.add_argument(
        "--exclude",
        default="",
        help="Comma-separated substrings to exclude (default: none)",
    )
    args = p.parse_args(argv)
    exclude = [s.strip().lower() for s in args.exclude.split(",") if s.strip()]

    ideas = build_report(
        Path(args.db),
        limit_conversations=args.limit_conversations,
        max_user_messages=args.max_user_messages,
        out_path=Path(args.out),
        mode=args.mode,
        exclude=exclude,
    )
    print(f"wrote {args.out} ({len(ideas)} ideas)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
