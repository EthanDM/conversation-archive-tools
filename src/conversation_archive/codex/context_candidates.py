#!/usr/bin/env python3
"""Produce a conservative, local review queue for portable context candidates.

Candidate claims require retained user-message evidence. Any ChatGPT overlap
check is only a local search signal and never asserts saved Memory state.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..chatgpt.paths import default_db_path as default_chatgpt_db_path, default_output_dir
from .paths import default_db_path


@dataclass(frozen=True)
class Rule:
    """One configurable candidate claim and the evidence pattern that supports it."""
    key: str
    proposed_text: str
    pattern: re.Pattern[str]
    overlap_query: str


DEFAULT_RULES = (
    Rule("collaboration", "Prefer direct, evidence-backed answers that distinguish confirmed facts from inference and preserve explicit scope.", re.compile(r"\b(direct|evidence[- ]backed|confirmed facts|inference|preserve (?:explicit )?scope)\b", re.I), '"evidence-backed" OR "confirmed facts"'),
    Rule("solution_design", "Prefer the smallest practical, maintainable solution over unnecessary abstraction or complexity.", re.compile(r"\b(simple|maintainable|low[- ]complexity|overengineer|smallest solution)\b", re.I), 'maintainable OR overengineer'),
    Rule("writing_boundary", "In personal writing, do not invent first-person biography, feelings, experiences, or beliefs; distinguish analysis from drafting.", re.compile(r"\b(do not invent|don'?t invent|authorship|first[- ]person|my (?:feelings|experiences|beliefs))\b", re.I), 'authorship OR "do not invent"'),
    Rule("local_first_tools", "For small personal tools, default to browser-only, local-first designs without accounts, API keys, or server infrastructure unless needed.", re.compile(r"\b(browser[- ]only|local[- ]first|no accounts|no api key|no server infrastructure)\b", re.I), '"browser-only" OR "no api key"'),
    Rule("visual_style", "Prefer restrained, low-noise presentation and practical utility over decorative dashboard complexity.", re.compile(r"\b(low[- ]noise|restrained|decorative dashboard|practical utility)\b", re.I), '"low-noise" OR restrained'),
)

SENSITIVE_PATTERNS = {
    "health": re.compile(r"\b(health|weight|calorie|doctor|symptom|diet|workout|skin)\b", re.I),
    "relationships": re.compile(r"\b(dating|relationship|breakup|lonely|family|feelings)\b", re.I),
    "finance_legal": re.compile(r"\b(finance|bank|tax|legal|settlement|salary|expense)\b", re.I),
    "political_personal": re.compile(r"\b(political|election|campaign|advocacy)\b", re.I),
}
OPERATIONAL_PATTERN = re.compile(r"\b(pr|pull request|ci|branch|merge|test failure|typescript|npm|python|sqlite error|deploy)\b", re.I)
EXPLICIT_PERSONAL_PATTERN = re.compile(r"\b(i (?:prefer|want|need|like|value|am|['’]m)|my (?:goal|preference|site|portfolio|writing)|for me|don'?t (?:invent|make))\b", re.I)
MAX_EVIDENCE_CHARS = 1_200


def _possible_overlap(chatgpt_db: Path | None, query: str) -> bool | None:
    if chatgpt_db is None or not chatgpt_db.exists():
        return None
    try:
        with sqlite3.connect(chatgpt_db) as connection:
            return connection.execute("SELECT 1 FROM messages_fts WHERE messages_fts MATCH ? LIMIT 1", (query,)).fetchone() is not None
    except sqlite3.Error:
        return None


def load_rules(path: Path | None) -> tuple[tuple[Rule, ...], dict[str, re.Pattern[str]]]:
    """Load trusted local candidate rules or return the generic built-in defaults.

    Custom JSON is configuration, not untrusted input: invalid schemas or regex
    patterns raise ``ValueError`` or ``re.error`` before any session is read.
    """
    if path is None:
        return DEFAULT_RULES, SENSITIVE_PATTERNS
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise ValueError("rules file must contain a rules array")
    rules: list[Rule] = []
    for item in data["rules"]:
        if not isinstance(item, dict):
            raise ValueError("each rule must be an object")
        values = [item.get(key) for key in ("key", "proposed_text", "pattern", "overlap_query")]
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("each rule needs key, proposed_text, pattern, and overlap_query strings")
        rules.append(Rule(values[0], values[1], re.compile(values[2], re.I), values[3]))
    if not rules or len({rule.key for rule in rules}) != len(rules):
        raise ValueError("rules must have unique keys")
    raw_patterns = data.get("sensitive_patterns", {})
    if not isinstance(raw_patterns, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in raw_patterns.items()):
        raise ValueError("sensitive_patterns must map names to regex strings")
    patterns = {key: re.compile(value, re.I) for key, value in raw_patterns.items()}
    return tuple(rules), patterns or SENSITIVE_PATTERNS


def build_candidates(codex_db: Path, chatgpt_db: Path | None, *, evidence_limit: int, rules: tuple[Rule, ...] = DEFAULT_RULES, sensitive_patterns: dict[str, re.Pattern[str]] = SENSITIVE_PATTERNS) -> dict[str, Any]:
    """Return source-linked candidate claims supported by user messages only.

    Work-like, oversized, and non-first-person messages are excluded before
    scoring. ``chatgpt_db`` may be absent; a positive overlap is deliberately
    tentative and does not change candidate evidence or confidence.
    """
    connection = sqlite3.connect(codex_db)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT m.id, m.session_id, m.create_time_iso, m.text, s.cwd, s.source_path
        FROM messages m JOIN sessions s ON s.session_id=m.session_id
        WHERE m.role='user' ORDER BY m.create_time_iso DESC, m.id DESC
        """
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    excluded_operational = 0
    excluded_nonpersonal = 0
    excluded_oversized = 0
    for row in rows:
        text = row["text"]
        if len(text) > MAX_EVIDENCE_CHARS:
            excluded_oversized += 1
            continue
        if not EXPLICIT_PERSONAL_PATTERN.search(text):
            excluded_nonpersonal += 1
            continue
        if OPERATIONAL_PATTERN.search(text) or "/work/" in (row["cwd"] or ""):
            excluded_operational += 1
            continue
        for rule in rules:
            if not rule.pattern.search(text):
                continue
            sensitivity = [name for name, pattern in sensitive_patterns.items() if pattern.search(text)]
            grouped[rule.key].append({
                "message_id": row["id"],
                "session_id": row["session_id"],
                "time": row["create_time_iso"],
                "cwd": row["cwd"],
                "source_path": row["source_path"],
                "text": text,
                "sensitivity": sensitivity,
            })
    candidates: list[dict[str, Any]] = []
    for rule in rules:
        evidence = grouped[rule.key]
        if not evidence:
            continue
        chosen = evidence[:evidence_limit]
        score = min(1.0, 0.45 + min(len(evidence), 4) * 0.12 + (0.07 if len(evidence) > 1 else 0))
        candidates.append({
            "key": rule.key,
            "proposed_text": rule.proposed_text,
            "confidence": "high" if score >= 0.75 else "medium",
            "score": round(score, 2),
            "evidence_count": len(evidence),
            "possible_chatgpt_overlap": _possible_overlap(chatgpt_db, rule.overlap_query),
            "sensitivity": sorted({name for item in chosen for name in item["sensitivity"]}),
            "evidence": chosen,
        })
    candidates.sort(key=lambda item: (-item["score"], item["key"]))
    return {
        "candidate_count": len(candidates),
        "candidates": candidates,
        "excluded": {
            "operational_messages": excluded_operational,
            "nonpersonal_messages": excluded_nonpersonal,
            "oversized_messages": excluded_oversized,
        },
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    """Write a human-reviewable candidate report to a caller-selected local path."""
    lines = [
        "# Codex Context Candidates",
        "",
        "This is a local review queue, not ChatGPT memory synchronization. Candidate claims are supported only by retained user messages; assistant messages are not used as evidence.",
        "",
    ]
    if not report["candidates"]:
        lines.append("No candidates matched the conservative rules. Expand rules or search the index directly.")
    for candidate in report["candidates"]:
        overlap = candidate["possible_chatgpt_overlap"]
        overlap_label = "possible overlap" if overlap else "no overlap signal" if overlap is False else "not checked"
        lines.extend([
            f"## {candidate['key'].replace('_', ' ').title()}",
            "",
            f"**Proposed context:** {candidate['proposed_text']}",
            "",
            f"Confidence: {candidate['confidence']} ({candidate['score']}); evidence: {candidate['evidence_count']} messages; ChatGPT export: {overlap_label}.",
        ])
        if candidate["sensitivity"]:
            lines.append(f"Sensitive flags: {', '.join(candidate['sensitivity'])}.")
        lines.append("")
        for evidence in candidate["evidence"]:
            lines.extend([
                f"- `{evidence['session_id']}` · {evidence['time'] or 'unknown time'} · `{evidence['cwd'] or 'unknown cwd'}`",
                f"  - {evidence['text']}",
                f"  - Source: `{evidence['source_path']}`",
            ])
        lines.append("")
    excluded = report["excluded"]
    lines.extend([
        "## Excluded Before Candidate Scoring",
        "",
        f"- Operational/work messages: {excluded['operational_messages']}",
        f"- Messages without an explicit personal signal: {excluded['nonpersonal_messages']}",
        f"- Oversized pasted content: {excluded['oversized_messages']}",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Generate a conservative, source-linked Codex context review pack.")
    parser.add_argument("--db", default=default_db_path(), help="Codex session SQLite database")
    parser.add_argument("--chatgpt-db", default=default_chatgpt_db_path(), help="Optional ChatGPT export database for tentative overlap signals")
    parser.add_argument("--rules", default=None, help="Optional JSON rules file; see examples/context_rules.example.json")
    parser.add_argument("--out", default=str(default_output_dir() / "codex-context-candidates.md"), help="Markdown review output")
    parser.add_argument("--json-out", default=str(default_output_dir() / "codex-context-candidates.json"), help="JSON review output")
    parser.add_argument("--evidence-limit", type=int, default=3, help="Maximum evidence messages per candidate")
    args = parser.parse_args(argv)
    codex_db = Path(args.db).expanduser()
    chatgpt_db = Path(args.chatgpt_db).expanduser() if args.chatgpt_db else None
    rules, sensitive_patterns = load_rules(Path(args.rules).expanduser() if args.rules else None)
    report = build_candidates(codex_db, chatgpt_db, evidence_limit=args.evidence_limit, rules=rules, sensitive_patterns=sensitive_patterns)
    write_markdown(report, Path(args.out))
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {report['candidate_count']} candidates to {args.out} and {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
