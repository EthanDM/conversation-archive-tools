#!/usr/bin/env python3
from __future__ import annotations

import argparse
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


WORK_TERMS = {
    "work",
    "career",
    "job",
    "role",
    "engineering",
    "software",
    "mobile",
    "ios",
    "android",
    "frontend",
    "backend",
    "fullstack",
    "product",
    "design",
    "team",
    "management",
    "leadership",
    "strategy",
    "startup",
    "business",
    "marketing",
    "growth",
    "metrics",
    "pricing",
    "sales",
    "client",
    "release",
    "bug",
    "architecture",
    "api",
    "database",
}


FRAMEWORK_TITLE_TERMS = {
    "framework",
    "principles",
    "playbook",
    "operating system",
    "system",
    "template",
    "checklist",
    "rubric",
    "scorecard",
    "strategy",
    "process",
    "guide",
    "rules",
    "policy",
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
        "plan",
        "schedule",
    ],
    "Learning & Research": [
        "summarize",
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

PROMPT_KEEP_TERMS = [
    "write",
    "draft",
    "rewrite",
    "outline",
    "summarize",
    "summary",
    "plan",
    "strategy",
    "framework",
    "template",
    "checklist",
    "rubric",
    "scorecard",
    "compare",
    "analysis",
    "analyze",
    "evaluate",
    "pros",
    "cons",
    "tradeoff",
    "brainstorm",
    "ideas",
    "generate",
    "list",
    "guide",
    "principles",
    "operating system",
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


def _rel_path(out_dir: Path, file_path: Path) -> str:
    try:
        rel = file_path.relative_to(out_dir.parent)
    except ValueError:
        rel = file_path
    return (Path("..") / rel).as_posix()


def _extract_first_user_prompt(body: str) -> Optional[str]:
    for role, _ts, text in _iter_sections(body):
        if role != "user":
            continue
        cleaned = " ".join(text.strip().split())
        if cleaned:
            return cleaned
    return None


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


def _normalize_prompt(prompt: str) -> str:
    p = prompt.lower()
    p = re.sub(r"[^a-z0-9\s]", " ", p)
    p = re.sub(r"\s+", " ", p).strip()
    if len(p) > 200:
        p = p[:200].strip()
    return p


def _is_structured_prompt(prompt: str) -> bool:
    p = prompt.lower()
    words = p.split()
    if len(words) < 10:
        return False
    return any(term in p for term in PROMPT_KEEP_TERMS)


def _collect_conversations(root: Path) -> List[Dict[str, object]]:
    conversations: List[Dict[str, object]] = []
    for path in root.rglob("*.md"):
        if "_indexes" in path.parts or "_phase3" in path.parts:
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
            }
        )
    return conversations


def _top_terms(convs: Sequence[Dict[str, object]], max_terms: int) -> List[Tuple[str, int]]:
    counter: Counter[str] = Counter()
    for conv in convs:
        counter.update(conv.get("terms") or [])
    return counter.most_common(max_terms)


def _top_titles(convs: Sequence[Dict[str, object]], max_items: int) -> List[Tuple[str, Path, str]]:
    items = []
    for conv in convs:
        title = conv.get("title") or "Untitled"
        date = conv.get("date") or ""
        items.append((date, Path(conv["path"]), title))
    items = sorted(items, key=lambda x: x[0], reverse=True)
    return items[:max_items]


def _write_themes(out_dir: Path, conversations: Sequence[Dict[str, object]]) -> None:
    by_para: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for conv in conversations:
        by_para[conv.get("para") or "Unknown"].append(conv)

    work_convs = []
    for conv in conversations:
        tokens = set(_tokenize(conv.get("title") or ""))
        tokens.update([t.lower() for t in conv.get("terms") or []])
        if tokens & WORK_TERMS:
            work_convs.append(conv)

    lines: List[str] = ["# Major Themes", ""]

    lines.append("## Work")
    top_work = _top_terms(work_convs, 10)
    if top_work:
        lines.append("- Top topics: " + ", ".join([t for t, _c in top_work]))
    for date, path, title in _top_titles(work_convs, 6):
        rel = _rel_path(out_dir, path)
        lines.append(f"- {date} — [{title}]({rel})")
    lines.append("")

    lines.append("## Projects")
    project_convs = by_para.get("Projects", [])
    top_projects = _top_terms(project_convs, 12)
    if top_projects:
        lines.append("- Top topics: " + ", ".join([t for t, _c in top_projects]))
    for date, path, title in _top_titles(project_convs, 8):
        rel = _rel_path(out_dir, path)
        lines.append(f"- {date} — [{title}]({rel})")
    lines.append("")

    lines.append("## Personal")
    personal_convs = by_para.get("Areas", [])
    top_personal = _top_terms(personal_convs, 12)
    if top_personal:
        lines.append("- Top topics: " + ", ".join([t for t, _c in top_personal]))
    for date, path, title in _top_titles(personal_convs, 8):
        rel = _rel_path(out_dir, path)
        lines.append(f"- {date} — [{title}]({rel})")
    lines.append("")

    (out_dir / "themes.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _write_prompts(out_dir: Path, conversations: Sequence[Dict[str, object]]) -> None:
    buckets: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    seen: set[str] = set()

    for conv in conversations:
        prompt = conv.get("prompt")
        if not prompt:
            continue
        if len(prompt) < 25 or len(prompt) > 500:
            continue
        if not _is_structured_prompt(prompt):
            continue
        category = _prompt_category(prompt)
        if not category:
            continue
        norm = _normalize_prompt(prompt)
        if norm in seen:
            continue
        seen.add(norm)
        buckets[category].append(
            {
                "prompt": prompt,
                "path": Path(conv["path"]),
                "title": conv.get("title") or "Untitled",
                "date": conv.get("date") or "",
            }
        )

    framework_convs: List[Dict[str, object]] = []
    for conv in conversations:
        title = (conv.get("title") or "").lower()
        if any(term in title for term in FRAMEWORK_TITLE_TERMS):
            framework_convs.append(conv)

    lines: List[str] = ["# Prompt & Framework Library", ""]
    lines.append("## Prompt Library (structured prompts)")
    for category in sorted(buckets.keys()):
        lines.append(f"### {category}")
        items = sorted(buckets[category], key=lambda x: x["date"], reverse=True)[:8]
        for item in items:
            rel = _rel_path(out_dir, item["path"])
            date = item["date"]
            title = item["title"]
            prompt = item["prompt"]
            lines.append(f"- {date} — [{title}]({rel})")
            lines.append(f"  Prompt: {prompt}")
        lines.append("")

    lines.append("## Framework Conversations (by title)")
    items = sorted(framework_convs, key=lambda x: x.get("date") or "", reverse=True)[:40]
    for conv in items:
        rel = _rel_path(out_dir, Path(conv["path"]))
        lines.append(f"- {conv.get('date')} — [{conv.get('title')}]({rel})")
    lines.append("")

    (out_dir / "prompts_and_frameworks.md").write_text(
        "\n".join(lines).strip() + "\n", encoding="utf-8"
    )


def _write_evolution(out_dir: Path, conversations: Sequence[Dict[str, object]]) -> None:
    by_year: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for conv in conversations:
        by_year[conv.get("year") or "unknown"].append(conv)

    years = sorted(by_year.keys())
    lines: List[str] = ["# Evolution Over Time", ""]
    for year in years:
        convs = by_year[year]
        total = len(convs)
        para_counts = Counter([c.get("para") or "Unknown" for c in convs])
        top_terms = _top_terms(convs, 8)
        lines.append(f"## {year}")
        lines.append(f"- Conversations: {total}")
        if para_counts:
            parts = ", ".join([f"{k}: {v}" for k, v in para_counts.most_common()])
            lines.append(f"- PARA mix: {parts}")
        if top_terms:
            lines.append("- Top topics: " + ", ".join([t for t, _c in top_terms]))
        lines.append("")

    (out_dir / "evolution.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _write_stats(out_dir: Path, conversations: Sequence[Dict[str, object]]) -> None:
    stats: Dict[str, object] = {}
    stats["total_conversations"] = len(conversations)
    stats["by_para"] = Counter([c.get("para") or "Unknown" for c in conversations])
    stats["by_year"] = Counter([c.get("year") or "unknown" for c in conversations])
    stats["top_terms_overall"] = _top_terms(conversations, 20)
    (out_dir / "phase3_stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=True), encoding="utf-8"
    )


def build_phase3(root: Path, out_dir: Path) -> None:
    conversations = _collect_conversations(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_themes(out_dir, conversations)
    _write_prompts(out_dir, conversations)
    _write_evolution(out_dir, conversations)
    _write_stats(out_dir, conversations)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build Phase 3 summaries from knowledge_base."
    )
    parser.add_argument("--root", default="knowledge_base")
    parser.add_argument("--out", default="knowledge_base/_phase3")
    args = parser.parse_args()

    root = Path(args.root)
    out_dir = Path(args.out)
    if not root.exists():
        print(f"knowledge base not found: {root}")
        return 1

    build_phase3(root, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
