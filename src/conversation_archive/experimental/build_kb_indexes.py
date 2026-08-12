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

DECISION_RE = re.compile(
    r"\b("
    r"i\s+(?:decide|decided|choose|chose|will|am going to|plan to|plan on|intend to|"
    r"settled on|finalized|commit to|go with)|"
    r"we\s+(?:decide|decided|choose|chose|will|are going to|plan to|plan on|intend to|"
    r"settled on|finalized|commit to|go with)|"
    r"let's|let us"
    r")\b",
    re.IGNORECASE,
)

INSIGHT_RE = re.compile(
    r"\b(learned|realized|insight|takeaway|lesson|principle|rule|heuristic|pattern|"
    r"clarify|key point)\b",
    re.IGNORECASE,
)


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
            # Potential list
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
        # Strip quotes if present
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


def _split_paragraphs(text: str) -> Iterable[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return parts


def _split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _extract_hits(
    body: str,
    regex: re.Pattern,
    max_hits: int,
    *,
    roles: Optional[set[str]] = None,
) -> List[Dict[str, object]]:
    hits: List[Dict[str, object]] = []
    for role, ts, section_text in _iter_sections(body):
        if roles and role not in roles:
            continue
        for para in _split_paragraphs(section_text):
            candidates = _split_sentences(para)
            for sent in candidates:
                if not regex.search(sent):
                    continue
                if "?" in sent:
                    continue
                lowered = sent.strip().lower()
                if lowered.startswith(("how do i", "what should i", "should i", "can i")):
                    continue
                words = sent.split()
                if len(words) < 6:
                    continue
                snippet = sent.replace("\n", " ").strip()
                if len(snippet) > 240:
                    snippet = snippet[:237].rstrip() + "..."
                hits.append({"role": role, "timestamp": ts or "", "snippet": snippet})
                if len(hits) >= max_hits:
                    return hits
    return hits


def _collect_conversations(root: Path) -> List[Dict[str, object]]:
    conversations: List[Dict[str, object]] = []
    for path in root.rglob("*.md"):
        if "_indexes" in path.parts:
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
        conversations.append(
            {
                "path": path,
                "title": str(front.get("title") or path.stem),
                "date": str(front.get("date") or ""),
                "created": str(front.get("created") or ""),
                "updated": str(front.get("updated") or ""),
                "para": str(front.get("para") or ""),
                "conversation_id": str(front.get("conversation_id") or ""),
                "topics": list(topics or []),
                "tags": list(tags or []),
                "terms": terms,
                "body": body_text,
            }
        )
    return conversations


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


def _write_topic_indexes(
    out_dir: Path,
    conversations: Sequence[Dict[str, object]],
    *,
    key: str,
    filename: str,
) -> None:
    index: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for conv in conversations:
        for term in conv.get(key, []) or []:
            index[str(term)].append(conv)

    lines: List[str] = [f"# {key.title()} Index", ""]
    for term in sorted(index.keys(), key=str.lower):
        lines.append(f"## {term}")
        items = sorted(index[term], key=lambda c: str(c.get("date") or ""), reverse=True)
        for item in items:
            rel = _rel_path(out_dir, Path(item["path"]))
            title = item.get("title") or "Untitled"
            date = item.get("date") or ""
            lines.append(f"- {date} — [{title}]({rel})")
        lines.append("")

    (out_dir / filename).write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _build_related(
    conversations: Sequence[Dict[str, object]], *, top_k: int
) -> Dict[str, List[Dict[str, object]]]:
    term_to_convs: Dict[str, List[int]] = defaultdict(list)
    for idx, conv in enumerate(conversations):
        for term in conv.get("terms", []) or []:
            term_to_convs[str(term)].append(idx)

    related: Dict[str, List[Dict[str, object]]] = {}
    for idx, conv in enumerate(conversations):
        terms = set(conv.get("terms") or [])
        if not terms:
            related[str(conv.get("conversation_id") or idx)] = []
            continue
        scores: Counter[int] = Counter()
        for term in terms:
            for other in term_to_convs.get(term, []):
                if other == idx:
                    continue
                scores[other] += 1
        if not scores:
            related[str(conv.get("conversation_id") or idx)] = []
            continue

        candidates = scores.most_common(top_k * 2)
        items: List[Tuple[int, float, List[str]]] = []
        for other_idx, shared_count in candidates:
            other_terms = set(conversations[other_idx].get("terms") or [])
            shared_terms = sorted(terms & other_terms)
            if not shared_terms:
                continue
            score = shared_count / max(1, len(terms))
            items.append((other_idx, score, shared_terms))
        items = sorted(items, key=lambda t: t[1], reverse=True)[:top_k]

        related[str(conv.get("conversation_id") or idx)] = [
            {
                "conversation_id": conversations[o].get("conversation_id") or "",
                "title": conversations[o].get("title") or "",
                "date": conversations[o].get("date") or "",
                "path": Path(conversations[o]["path"]).as_posix(),
                "score": round(score, 4),
                "shared_terms": shared_terms[:8],
            }
            for o, score, shared_terms in items
        ]
    return related


def _write_related_md(out_dir: Path, related: Dict[str, List[Dict[str, object]]]) -> None:
    lines = ["# Related Conversations", ""]
    for conv_id, items in related.items():
        if not items:
            continue
        lines.append(f"## {conv_id}")
        for item in items:
            title = item.get("title") or "Untitled"
            date = item.get("date") or ""
            path = _rel_path(out_dir, Path(item.get("path") or ""))
            score = item.get("score")
            shared = ", ".join(item.get("shared_terms") or [])
            lines.append(f"- {date} — [{title}]({path}) (score {score}, shared: {shared})")
        lines.append("")
    (out_dir / "related.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _extract_items(
    conversations: Sequence[Dict[str, object]],
    *,
    regex: re.Pattern,
    max_per_conv: int,
    roles: Optional[set[str]] = None,
) -> List[Dict[str, object]]:
    items: List[Dict[str, object]] = []
    for conv in conversations:
        body = conv.get("body") or ""
        hits = _extract_hits(
            body,
            regex,
            max_per_conv,
            roles=roles,
        )
        if not hits:
            continue
        for hit in hits:
            role = hit.get("role") or ""
            ts = hit.get("timestamp") or ""
            snippet = hit.get("snippet") or ""
            items.append(
                {
                    "conversation_id": conv.get("conversation_id") or "",
                    "title": conv.get("title") or "",
                    "date": conv.get("date") or "",
                    "role": role,
                    "timestamp": ts,
                    "snippet": snippet,
                    "path": Path(conv["path"]).as_posix(),
                }
            )
    return items


def _write_items(
    out_dir: Path, items: Sequence[Dict[str, object]], filename: str, title: str
) -> None:
    lines = [f"# {title}", ""]
    for item in sorted(items, key=lambda x: (x.get("date") or "", x.get("title") or "")):
        date = item.get("date") or ""
        conv_title = item.get("title") or "Untitled"
        role = item.get("role") or ""
        snippet = item.get("snippet") or ""
        path = _rel_path(out_dir, Path(item.get("path") or ""))
        lines.append(f"- {date} — [{conv_title}]({path}) — {role}: {snippet}")
    (out_dir / filename).write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _write_items_csv(out_dir: Path, items: Sequence[Dict[str, object]], filename: str) -> None:
    if not items:
        return
    path = out_dir / filename
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "date",
                "title",
                "role",
                "timestamp",
                "snippet",
                "path",
                "conversation_id",
            ],
        )
        writer.writeheader()
        for item in items:
            writer.writerow(
                {
                    "date": item.get("date") or "",
                    "title": item.get("title") or "",
                    "role": item.get("role") or "",
                    "timestamp": item.get("timestamp") or "",
                    "snippet": item.get("snippet") or "",
                    "path": item.get("path") or "",
                    "conversation_id": item.get("conversation_id") or "",
                }
            )


def _write_conversation_index(
    out_dir: Path, conversations: Sequence[Dict[str, object]]
) -> None:
    path = out_dir / "conversations.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "date",
                "title",
                "para",
                "path",
                "conversation_id",
                "topics",
                "tags",
            ],
        )
        writer.writeheader()
        for conv in conversations:
            writer.writerow(
                {
                    "date": conv.get("date") or "",
                    "title": conv.get("title") or "",
                    "para": conv.get("para") or "",
                    "path": Path(conv["path"]).as_posix(),
                    "conversation_id": conv.get("conversation_id") or "",
                    "topics": ",".join(conv.get("topics") or []),
                    "tags": ",".join(conv.get("tags") or []),
                }
            )


def build_indexes(root: Path, out_dir: Path, *, max_per_conv: int, top_k: int) -> None:
    conversations = _collect_conversations(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_topic_indexes(out_dir, conversations, key="topics", filename="topics.md")
    _write_topic_indexes(out_dir, conversations, key="tags", filename="tags.md")
    _write_conversation_index(out_dir, conversations)

    decisions = _extract_items(
        conversations,
        regex=DECISION_RE,
        max_per_conv=max_per_conv,
        roles={"user"},
    )
    insights = _extract_items(
        conversations, regex=INSIGHT_RE, max_per_conv=max_per_conv
    )
    _write_items(out_dir, decisions, "decisions.md", "Decision Log")
    _write_items_csv(out_dir, decisions, "decisions.csv")
    _write_items(out_dir, insights, "insights.md", "Insights Log")
    _write_items_csv(out_dir, insights, "insights.csv")

    related = _build_related(conversations, top_k=top_k)
    (out_dir / "related.json").write_text(
        json.dumps(related, indent=2, ensure_ascii=True), encoding="utf-8"
    )
    _write_related_md(out_dir, related)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build topic indexes, decision log, and related-conversation map."
    )
    parser.add_argument("--root", default="knowledge_base")
    parser.add_argument("--out", default="knowledge_base/_indexes")
    parser.add_argument("--max-per-conv", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    root = Path(args.root)
    out_dir = Path(args.out)
    if not root.exists():
        print(f"knowledge base not found: {root}")
        return 1

    build_indexes(root, out_dir, max_per_conv=args.max_per_conv, top_k=args.top_k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
