"""Inspect ChatGPT export structure and aggregate risk signals without indexing it."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from .index import _extract_zip_export, _iter_json_array, _resolve_input_paths

SECRET_PATTERN = re.compile(r"(?:AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|xox[baprs]-)")
SENSITIVE_PATTERN = re.compile(r"\b(?:health|medical|diagnosis|relationship|dating|tax|legal|political)\b", re.I)


def audit_export(input_path: Path) -> dict[str, Any]:
    """Return a no-write structural report for an export ZIP, directory, or JSON file.

    Secret and sensitive signals are aggregate record counts only; this function
    never prints or persists matching conversation content.
    """
    def inspect(directory: Path) -> dict[str, Any]:
        files = _resolve_input_paths(directory)
        records = conversations = malformed = secret_records = sensitive_records = 0
        for path in files:
            with path.open(encoding="utf-8") as handle:
                for item in _iter_json_array(handle):
                    records += 1
                    serialized = json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else ""
                    secret_records += int(bool(SECRET_PATTERN.search(serialized)))
                    sensitive_records += int(bool(SENSITIVE_PATTERN.search(serialized)))
                    if isinstance(item, dict) and isinstance(item.get("conversation_id") or item.get("id"), str):
                        conversations += 1
                    else:
                        malformed += 1
        return {"input": str(input_path), "source_files": [path.name for path in files], "records": records, "conversations": conversations, "malformed_records": malformed, "potential_secret_records": secret_records, "potential_sensitive_records": sensitive_records, "writes_data": False}

    if input_path.suffix.lower() != ".zip":
        return inspect(input_path)
    with tempfile.TemporaryDirectory(prefix="conversation-archive-audit-") as temporary:
        directory = Path(temporary)
        _extract_zip_export(input_path, directory)
        return inspect(directory)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Dry-run a ChatGPT export and report recognized conversation data without creating an index.")
    parser.add_argument("--input", required=True, help="Export ZIP, directory, conversations.json, or shard glob")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a short text report")
    args = parser.parse_args(argv)
    report = audit_export(Path(args.input).expanduser())
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"recognized {report['conversations']} conversations from {len(report['source_files'])} file(s)")
        print(f"malformed: {report['malformed_records']}; possible secrets: {report['potential_secret_records']}; possible sensitive records: {report['potential_sensitive_records']}; writes data: no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
