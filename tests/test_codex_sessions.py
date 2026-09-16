from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_DIR))

from conversation_archive.codex.context_candidates import build_candidates, load_rules
from conversation_archive.codex import index as codex_index
from conversation_archive.codex.publish import publish_sessions
from conversation_archive.codex.search import search
from conversation_archive.codex.session_index import index_sessions, parse_session_file


def event(event_type: str, payload: dict, timestamp: str = "2026-08-12T12:00:00Z") -> str:
    return json.dumps({"timestamp": timestamp, "type": event_type, "payload": payload}) + "\n"


def message(role: str, text: str, timestamp: str = "2026-08-12T12:01:00Z") -> str:
    content_type = "input_text" if role == "user" else "output_text"
    return event("response_item", {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]}, timestamp)


class CodexSessionIndexTests(unittest.TestCase):
    def write_fixture(self, directory: Path, name: str = "session.jsonl") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
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

    def test_publisher_uses_machine_namespaces_and_shared_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            desktop_sessions = root / "desktop-sessions"
            neo_sessions = root / "neo-sessions"
            desktop_source = self.write_fixture(desktop_sessions / "2026" / "09")
            neo_source = self.write_fixture(neo_sessions, "neo.jsonl")
            neo_source.write_text(
                neo_source.read_text(encoding="utf-8").replace('"session-1"', '"session-2"'),
                encoding="utf-8",
            )
            archive = root / "shared-archive"

            first = publish_sessions(desktop_sessions, archive, "desktop")
            second = publish_sessions(neo_sessions, archive, "neo")
            self.assertEqual((first.copied, first.skipped), (1, 0))
            self.assertEqual((second.copied, second.skipped), (1, 0))
            self.assertEqual(first.destination, (archive / "desktop").resolve())
            self.assertTrue((archive / "desktop" / "2026" / "09" / desktop_source.name).exists())

            shared_db = root / "shared.sqlite"
            indexed, skipped, retained = index_sessions(archive, shared_db)
            self.assertEqual((indexed, skipped, retained), (2, 0, 6))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(search(shared_db, '"browser-only"', limit=5, context=0, width=80, lines=3), 0)
            self.assertIn("session=session-1", output.getvalue())
            self.assertIn("session=session-2", output.getvalue())

            repeated = publish_sessions(desktop_sessions, archive, "desktop")
            self.assertEqual((repeated.copied, repeated.skipped), (0, 1))

            desktop_source.write_text(
                desktop_source.read_text(encoding="utf-8") + message("user", "An updated request."),
                encoding="utf-8",
            )
            updated = publish_sessions(desktop_sessions, archive, "desktop")
            self.assertEqual((updated.copied, updated.skipped), (1, 0))

    def test_publisher_rejects_invalid_machine_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            self.write_fixture(sessions)
            with self.assertRaisesRegex(ValueError, "Machine ID"):
                publish_sessions(sessions, root / "archive", "../neo")

    def test_publisher_rejects_an_archive_inside_the_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            self.write_fixture(sessions)
            with self.assertRaisesRegex(ValueError, "must not be inside its input"):
                publish_sessions(sessions, sessions / "archive", "desktop")

    def test_shared_index_rejects_explicit_input(self) -> None:
        with self.assertRaises(SystemExit):
            codex_index.main(["--shared", "--input", "sessions"])

    def test_shared_index_uses_the_configured_shared_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_fixture(root / "shared" / "desktop")
            db = root / "index.sqlite"
            with patch.dict(
                "os.environ",
                {"CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT": str(root / "shared")},
                clear=False,
            ):
                self.assertEqual(codex_index.main(["--shared", "--db", str(db)]), 0)
            self.assertTrue(db.exists())

    def test_missing_shared_input_does_not_reset_an_existing_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            self.write_fixture(sessions)
            db = root / "index.sqlite"
            index_sessions(sessions, db)
            with patch.dict(
                "os.environ",
                {"CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT": str(root / "missing")},
                clear=False,
            ):
                with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
                    codex_index.main(["--shared", "--reset", "--db", str(db)])
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
