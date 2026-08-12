#!/usr/bin/env python3
"""Reject private runtime data and credentials in publishable repository files.

The audit examines tracked files plus unignored working-tree additions. Including
the latter catches a leak before the file is staged; CI naturally sees only the
checked-out repository content.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, NamedTuple


class Finding(NamedTuple):
    path: Path
    reason: str


FORBIDDEN_FILENAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    "auth.json",
    "credentials.json",
    "token.json",
    "profile-snapshot.json",
    "user-preferences.json",
    "interaction-log.ndjson",
    "conversation-history.json",
}
FORBIDDEN_SUFFIXES = {".sqlite", ".sqlite-shm", ".sqlite-wal"}
CONTENT_CHECKS = (
    (
        "personal home path",
        re.compile(
            r"(?:/" + r"Users/[^/\s]+(?=/|\s|[\"'`]|$)|/" + r"home/[^/\s]+(?=/|\s|[\"'`]|$)|[A-Za-z]:\\Users\\[^\\\s]+(?=\\|\s|[\"'`]|$))",
            re.IGNORECASE,
        ),
    ),
    ("email address", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)),
    ("GitHub token", re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)\b")),
    ("OpenAI API key", re.compile(r"\bsk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{20,}\b")),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    (
        "credential assignment",
        re.compile(
            r"(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|CLIENT_SECRET|PASSWORD)\s*=\s*(?!your_|example|replace|<)[^\s#]+",
            re.IGNORECASE,
        ),
    ),
)


def repository_files(root: Path) -> list[Path]:
    """Return tracked files and unignored additions, excluding deleted entries."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return sorted(
        (root / value.decode("utf-8"))
        for value in result.stdout.split(b"\0")
        if value and (root / value.decode("utf-8")).is_file()
    )


def audit_paths(root: Path, paths: Iterable[Path]) -> list[Finding]:
    """Return publication-boundary violations in paths relative to *root*."""
    findings: list[Finding] = []
    for path in paths:
        relative_path = path.relative_to(root)
        name = path.name.lower()
        if name in FORBIDDEN_FILENAMES or any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            findings.append(Finding(relative_path, "forbidden runtime-state filename"))
            continue

        content = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in CONTENT_CHECKS:
            if pattern.search(content):
                findings.append(Finding(relative_path, label))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check publishable files for private data and credentials.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()

    try:
        findings = audit_paths(root, repository_files(root))
    except subprocess.CalledProcessError as error:
        print(error.stderr.decode("utf-8", errors="replace"), file=sys.stderr, end="")
        return error.returncode or 1

    if findings:
        for finding in findings:
            print(f"{finding.path}: {finding.reason}", file=sys.stderr)
        return 1

    print("Release privacy audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
