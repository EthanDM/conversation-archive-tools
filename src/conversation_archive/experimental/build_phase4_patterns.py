#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


FRONTMATTER_BOUNDARY = "---"
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'-]{2,}")

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

PROMPT_CATEGORIES = {
    "Writing & Comms": [
        "write",
        "draft",
        "rewrite",
        "email",
        "message",
        "announcement",
        "script",
        "copy",
        "pitch",
        "bio",
        "headline",
    ],
    "Planning & Strategy": [
        "strategy",
        "roadmap",
        "plan",
        "planning",
        "go to market",
        "gtm",
        "pricing",
        "business",
        "model",
        "launch",
    ],
    "Decision & Analysis": [
        "decide",
        "decision",
        "compare",
        "pros",
        "cons",
        "tradeoff",
        "evaluate",
        "analysis",
        "recommend",
        "choose",
    ],
    "Product & Engineering": [
        "spec",
        "architecture",
        "api",
        "database",
        "schema",
        "bug",
        "fix",
        "estimate",
        "implementation",
        "technical",
        "design system",
    ],
    "Personal Systems": [
        "routine",
        "habit",
        "discipline",
        "goals",
        "operating system",
        "values",
        "principles",
        "schedule",
    ],
    "Learning & Research": [
        "summarize",
        "summary",
        "explain",
        "what is",
        "how to",
        "guide",
        "tutorial",
        "learn",
        "overview",
    ],
    "Creative & Ideation": [
        "brainstorm",
        "ideas",
        "story",
        "joke",
        "poem",
        "names",
        "tagline",
        "concept",
    ],
}

PROMPT_FLAGS = {
    "format": ["bullet", "bullets", "table", "format", "template", "checklist", "rubric"],
    "length": ["short", "concise", "long", "detailed", "thorough"],
    "steps": ["step", "steps", "walkthrough", "guide me", "process"],
    "examples": ["example", "examples"],
    "tradeoffs": ["pros", "cons", "tradeoff", "trade-offs"],
    "constraints": ["limit", "at least", "no more than", "exactly", "max", "minimum"],
    "summary": ["summarize", "summary", "tl;dr"],
    "decision": ["decide", "decision", "choose", "recommend"],
}

REQUEST_VERBS = [
    "write",
    "draft",
    "summarize",
    "explain",
    "compare",
    "list",
    "give",
    "provide",
    "generate",
    "analyze",
    "evaluate",
    "brainstorm",
    "recommend",
]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_frontmatter(text: str) -> Tuple[Dict[str, object], int]:
    if not text.startswith(FRONTMATTER_BOUNDARY):
        return {}, 0
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_BOUNDARY:
        return {}, 0
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_BOUNDARY:
            end_idx = i
            break
    if end_idx is None:
        return {}, 0

    data: Dict[str, object] = {}
    i = 1
    while i < end_idx:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        value = raw.strip()
        if value == "":
            items: List[str] = []
            i += 1
            while i < end_idx:
                list_line = lines[i]
                if list_line.strip().startswith("- "):
                    items.append(list_line.strip()[2:].strip())
                    i += 1
                    continue
                if list_line.startswith("  - "):
                    items.append(list_line.strip()[2:].strip())
                    i += 1
                    continue
                break
            data[key] = items
            continue
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1].replace('\\"', '"')
        data[key] = value
        i += 1
    return data, end_idx + 1


def _iter_sections(body: str) -> Iterable[Tuple[str, Optional[str], str]]:
    lines = body.splitlines()
    role = "unknown"
    ts = None
    buf: List[str] = []

    def flush() -> Iterable[Tuple[str, Optional[str], str]]:
        nonlocal buf
        if not buf:
            return []
        text = "\n".join(buf).strip()
        buf = []
        if not text:
            return []
        return [(role, ts, text)]

    for line in lines:
        if line.startswith("## "):
            for item in flush():
                yield item
            header = line[3:].strip()
            m = re.match(r"^(\w+)(?:\s+\(([^)]+)\))?$", header)
            if m:
                role = m.group(1)
                ts = m.group(2)
            else:
                role = header.split(" ", 1)[0]
                ts = None
            continue
        buf.append(line)

    for item in flush():
        yield item


def _extract_first_user_prompt(body: str) -> Optional[str]:
    for role, _ts, text in _iter_sections(body):
        if role != "user":
            continue
        cleaned = " ".join(text.strip().split())
        if cleaned:
            return cleaned
    return None


def _tokenize(text: str) -> List[str]:
    out: List[str] = []
    for tok in TOKEN_RE.findall(text.lower()):
        if tok in STOPWORDS:
            continue
        if tok.isdigit():
            continue
        out.append(tok)
    return out


def _is_bad_term(term: str) -> bool:
    t = term.strip().lower()
    if not t:
        return True
    if len(t) < 3:
        return True
    if re.match(r"^[0-9a-f-]{16,}$", t):
        return True
    if re.search(r"[0-9a-f]{8,}", t) and "-" in t:
        return True
    digits = sum(c.isdigit() for c in t)
    if digits >= 4:
        return True
    if len(t) > 12 and digits / max(1, len(t)) >= 0.35:
        return True
    return False


def _prompt_score(prompt: str, category_terms: Sequence[str]) -> int:
    prompt_l = prompt.lower()
    score = 0
    for term in category_terms:
        if term in prompt_l:
            score += 2
    return score


def _prompt_category(prompt: str) -> Optional[str]:
    best = None
    best_score = 0
    for cat, terms in PROMPT_CATEGORIES.items():
        score = _prompt_score(prompt, terms)
        if score > best_score:
            best_score = score
            best = cat
    return best if best_score > 0 else None


def _prompt_flags(prompt: str) -> Dict[str, bool]:
    p = prompt.lower()
    flags = {}
    for name, terms in PROMPT_FLAGS.items():
        flags[name] = any(term in p for term in terms)
    flags["has_numbers"] = bool(re.search(r"\d", p))
    return flags


def _request_verb(prompt: str) -> Optional[str]:
    p = prompt.lower()
    for verb in REQUEST_VERBS:
        if verb in p:
            return verb
    return None


def _collect_conversations(root: Path) -> List[Dict[str, object]]:
    conversations: List[Dict[str, object]] = []
    for path in root.rglob("*.md"):
        if "_indexes" in path.parts or "_phase3" in path.parts or "_phase4" in path.parts:
            continue
        text = _read_text(path)
        front, body_start = _parse_frontmatter(text)
        body = text.splitlines()[body_start:]
        body_text = "\n".join(body)
        topics = front.get("topics") or []
        tags = front.get("tags") or []
        if isinstance(topics, str):
            topics = [topics]
        if isinstance(tags, str):
            tags = [tags]
        topics = [t for t in topics if not _is_bad_term(str(t))]
        tags = [t for t in tags if not _is_bad_term(str(t))]
        terms = sorted({*(topics or []), *(tags or [])})
        date = str(front.get("date") or "")
        year = date[:4] if date else "unknown"
        title = str(front.get("title") or path.stem)
        prompt = _extract_first_user_prompt(body_text)
        category = _prompt_category(prompt) if prompt else None
        flags = _prompt_flags(prompt) if prompt else {}
        verb = _request_verb(prompt) if prompt else None
        word_count = len(prompt.split()) if prompt else 0
        conversations.append(
            {
                "path": path,
                "title": title,
                "date": date,
                "year": year,
                "para": str(front.get("para") or ""),
                "conversation_id": str(front.get("conversation_id") or ""),
                "topics": topics,
                "tags": tags,
                "terms": terms,
                "prompt": prompt,
                "prompt_category": category,
                "prompt_flags": flags,
                "prompt_word_count": word_count,
                "request_verb": verb,
            }
        )
    return conversations


def _load_decisions(decisions_csv: Path) -> List[Dict[str, str]]:
    if not decisions_csv.exists():
        return []
    items = []
    with decisions_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            items.append(row)
    return items


def _write_patterns(
    out_dir: Path,
    conversations: Sequence[Dict[str, object]],
    decisions: Sequence[Dict[str, str]],
) -> Dict[str, object]:
    stats: Dict[str, object] = {}
    total = len(conversations)
    stats["total_conversations"] = total

    by_para = Counter([c.get("para") or "Unknown" for c in conversations])
    by_year = Counter([c.get("year") or "unknown" for c in conversations])
    stats["by_para"] = by_para
    stats["by_year"] = by_year

    topic_counts = Counter()
    topic_by_para: Dict[str, Counter[str]] = defaultdict(Counter)
    topic_by_year: Dict[str, Counter[str]] = defaultdict(Counter)
    for conv in conversations:
        terms = conv.get("terms") or []
        topic_counts.update(terms)
        topic_by_para[conv.get("para") or "Unknown"].update(terms)
        topic_by_year[conv.get("year") or "unknown"].update(terms)

    top_overall = topic_counts.most_common(25)
    stats["top_topics_overall"] = top_overall

    trend_scores: List[Tuple[str, int]] = []
    years = sorted([y for y in by_year.keys() if y != "unknown"])
    if len(years) >= 2:
        first = years[0]
        last = years[-1]
        for topic in topic_counts.keys():
            delta = topic_by_year[last][topic] - topic_by_year[first][topic]
            trend_scores.append((topic, delta))
    trend_scores = sorted(trend_scores, key=lambda x: x[1], reverse=True)

    prompt_categories = Counter(
        [c.get("prompt_category") or "Uncategorized" for c in conversations if c.get("prompt")]
    )
    stats["prompt_categories"] = prompt_categories

    prompt_flags = Counter()
    verb_counts = Counter()
    prompt_words = []
    for conv in conversations:
        if not conv.get("prompt"):
            continue
        flags = conv.get("prompt_flags") or {}
        for k, v in flags.items():
            if v:
                prompt_flags[k] += 1
        verb = conv.get("request_verb")
        if verb:
            verb_counts[verb] += 1
        prompt_words.append(conv.get("prompt_word_count") or 0)

    stats["prompt_flags"] = prompt_flags
    stats["request_verbs"] = verb_counts
    stats["avg_prompt_words"] = round(sum(prompt_words) / max(1, len(prompt_words)), 2)

    conv_by_path = {Path(c["path"]).as_posix(): c for c in conversations}
    decisions_by_year = Counter()
    decisions_by_para = Counter()
    decision_verbs = Counter()
    for item in decisions:
        path = item.get("path") or ""
        conv = conv_by_path.get(path)
        if conv:
            decisions_by_year[conv.get("year") or "unknown"] += 1
            decisions_by_para[conv.get("para") or "Unknown"] += 1
        snippet = (item.get("snippet") or "").lower()
        for verb in [
            "decide",
            "decided",
            "choose",
            "chose",
            "go with",
            "settle",
            "finalize",
            "commit",
            "plan to",
            "will",
        ]:
            if verb in snippet:
                decision_verbs[verb] += 1

    stats["decisions_by_year"] = decisions_by_year
    stats["decisions_by_para"] = decisions_by_para
    stats["decision_verbs"] = decision_verbs

    lines: List[str] = ["# Phase 4 — Patterns", ""]
    lines.append("## What you talk about most")
    lines.append("- Top topics overall: " + ", ".join([t for t, _c in top_overall]))
    for para, counts in topic_by_para.items():
        top = counts.most_common(8)
        if not top:
            continue
        lines.append(f"- {para}: " + ", ".join([t for t, _c in top]))
    lines.append("")

    lines.append("## Decision-making patterns (heuristic)")
    if decisions_by_year:
        parts = ", ".join([f"{k}: {v}" for k, v in decisions_by_year.most_common()])
        lines.append(f"- Decisions by year: {parts}")
    if decisions_by_para:
        parts = ", ".join([f"{k}: {v}" for k, v in decisions_by_para.most_common()])
        lines.append(f"- Decisions by PARA: {parts}")
    if decision_verbs:
        parts = ", ".join([f"{k}: {v}" for k, v in decision_verbs.most_common(8)])
        lines.append(f"- Decision phrasing: {parts}")
    lines.append("- Note: decisions are extracted from user snippets; wording noise is expected.")
    lines.append("")

    lines.append("## Thinking evolution")
    for year in years:
        top = topic_by_year[year].most_common(6)
        top_str = ", ".join([t for t, _c in top]) if top else "n/a"
        lines.append(f"- {year}: top topics — {top_str}")
    if trend_scores:
        up = [t for t, d in trend_scores if d > 0][:8]
        down = [t for t, d in sorted(trend_scores, key=lambda x: x[1]) if d < 0][:8]
        if up:
            lines.append("- Topics trending up (first→last year): " + ", ".join(up))
        if down:
            lines.append("- Topics trending down (first→last year): " + ", ".join(down))
    lines.append("")

    lines.append("## AI collaboration style")
    total_prompts = len([c for c in conversations if c.get("prompt")])
    if total_prompts:
        def pct(n: int) -> str:
            return f"{round((n / total_prompts) * 100, 1)}%" if total_prompts else "0%"

        lines.append(f"- Average prompt length: {stats['avg_prompt_words']} words")
        for flag, count in prompt_flags.most_common():
            lines.append(f"- {flag} prompts: {pct(count)}")
        if prompt_categories:
            top_cats = ", ".join([f"{k}: {v}" for k, v in prompt_categories.most_common(6)])
            lines.append(f"- Top prompt intents: {top_cats}")
        if verb_counts:
            top_verbs = ", ".join([f"{k}: {v}" for k, v in verb_counts.most_common(6)])
            lines.append(f"- Common request verbs: {top_verbs}")
    lines.append("")

    (out_dir / "patterns.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return stats


def build_phase4(root: Path, out_dir: Path, indexes_dir: Path) -> None:
    conversations = _collect_conversations(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    decisions = _load_decisions(indexes_dir / "decisions.csv")
    stats = _write_patterns(out_dir, conversations, decisions)

    (out_dir / "phase4_stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=True), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Phase 4 pattern analysis.")
    parser.add_argument("--root", default="knowledge_base")
    parser.add_argument("--out", default="knowledge_base/_phase4")
    parser.add_argument("--indexes", default="knowledge_base/_indexes")
    args = parser.parse_args()

    root = Path(args.root)
    out_dir = Path(args.out)
    indexes_dir = Path(args.indexes)
    if not root.exists():
        print(f"knowledge base not found: {root}")
        return 1

    build_phase4(root, out_dir, indexes_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
