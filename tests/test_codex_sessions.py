from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_DIR))

from conversation_archive.codex.context_candidates import build_candidates, load_rules
from conversation_archive.codex.search import search
from conversation_archive.codex.session_index import index_sessions, parse_session_file


def event(event_type: str, payload: dict, timestamp: str = "2026-08-12T12:00:00Z") -> str:
    return json.dumps({"timestamp": timestamp, "type": event_type, "payload": payload}) + "\n"


def message(role: str, text: str, timestamp: str = "2026-08-12T12:01:00Z") -> str:
    content_type = "input_text" if role == "user" else "output_text"
    return event("response_item", {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]}, timestamp)


class CodexSessionIndexTests(unittest.TestCase):
    def write_fixture(self, directory: Path, name: str = "session.jsonl") -> Path:
        path = directory / name
        path.write_text(
            event("session_meta", {"id": "session-1", "cwd": "/personal/project"})
            + event("session_meta", {"id": "session-1", "cwd": "/personal/project"})
            + message("user", "<environment_context>ignore me</environment_context>")
            + message("user", "<skill>injected skill instructions</skill>")
            + message("developer", "developer instructions")
            + message("user", "I prefer browser-only tools with no API key.")
            + message("assistant", "That is a sensible local-first boundary.")
            + event("response_item", {"type": "function_call", "name": "shell", "arguments": "secret"})
            + message("assistant", "I prefer direct evidence-backed answers.", "2026-08-12T12:02:00Z"),
            encoding="utf-8",
        )
        return path

    def test_parser_retains_only_meaningful_user_and_assistant_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_fixture(Path(temporary))
            parsed = parse_session_file(path)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.session_id, "session-1")
        self.assertEqual([item.role for item in parsed.messages], ["user", "assistant", "assistant"])
        self.assertNotIn("environment_context", " ".join(item.text for item in parsed.messages))

    def test_parser_skips_malformed_jsonl_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_fixture(Path(temporary))
            path.write_text("{not json}\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
            parsed = parse_session_file(path)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(len(parsed.messages), 3)

    def test_example_rules_are_loadable(self) -> None:
        rules_path = Path(__file__).resolve().parents[1] / "examples" / "context_rules.example.json"
        rules, sensitive_patterns = load_rules(rules_path)
        self.assertEqual(rules[0].key, "collaboration")
        self.assertIn("health", sensitive_patterns)

    def test_incremental_index_search_and_context_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_fixture(root)
            db = root / "codex.sqlite"
            indexed, skipped, retained = index_sessions(root, db)
            self.assertEqual((indexed, skipped, retained), (1, 0, 3))
            indexed, skipped, retained = index_sessions(root, db)
            self.assertEqual((indexed, skipped, retained), (0, 1, 0))

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(search(db, '"browser-only"', limit=5, context=1, width=80, lines=3), 0)
            self.assertIn("session=session-1", output.getvalue())
            self.assertIn("source:", output.getvalue())

            report = build_candidates(db, None, evidence_limit=3)
            by_key = {candidate["key"]: candidate for candidate in report["candidates"]}
            self.assertIn("local_first_tools", by_key)
            self.assertNotIn("collaboration", by_key, "assistant-only phrasing must not produce a candidate")

    def test_changed_source_replaces_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.write_fixture(root)
            db = root / "codex.sqlite"
            index_sessions(root, db)
            path.write_text(event("session_meta", {"id": "session-1", "cwd": "/personal/project"}) + message("user", "A replacement request."), encoding="utf-8")
            indexed, skipped, retained = index_sessions(root, db)
            self.assertEqual((indexed, skipped, retained), (1, 0, 1))
            import sqlite3
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT text FROM messages").fetchone()[0], "A replacement request.")


if __name__ == "__main__":
    unittest.main()
