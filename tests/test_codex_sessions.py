from __future__ import annotations

import io
import json
import os
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
from conversation_archive.codex import session_index
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

    def test_publisher_skips_symlinks_outside_the_selected_sessions_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            outside = self.write_fixture(root / "outside")
            sessions.mkdir()
            (sessions / "linked.jsonl").symlink_to(outside)
            result = publish_sessions(sessions, root / "archive", "desktop")
            self.assertEqual((result.copied, result.skipped), (0, 0))
            self.assertFalse((result.destination / "linked.jsonl").exists())

    def test_publisher_rejects_symlinked_destination_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            self.write_fixture(sessions)
            archive = root / "archive"
            archive.mkdir()
            (archive / "desktop").symlink_to(root / "outside")
            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                publish_sessions(sessions, archive, "desktop")

    def test_publisher_preserves_source_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            source = self.write_fixture(sessions)
            source.chmod(0o600)
            result = publish_sessions(sessions, root / "archive", "desktop")
            destination = result.destination / source.name
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_publisher_copies_read_only_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            source = self.write_fixture(sessions)
            source.chmod(0o400)
            result = publish_sessions(sessions, root / "archive", "desktop")
            destination = result.destination / source.name
            self.assertEqual((result.copied, result.skipped), (1, 0))
            self.assertEqual(destination.stat().st_mode & 0o777, 0o400)

    def test_publisher_updates_permission_only_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            source = self.write_fixture(sessions)
            archive = root / "archive"
            publish_sessions(sessions, archive, "desktop")
            source.chmod(0o600)
            result = publish_sessions(sessions, archive, "desktop")
            destination = result.destination / source.name
            self.assertEqual((result.copied, result.skipped), (1, 0))
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_publisher_rejects_a_symlinked_destination_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            source = self.write_fixture(sessions)
            archive = root / "archive"
            destination = archive / "desktop" / source.name
            destination.parent.mkdir(parents=True)
            destination.symlink_to(source)
            with self.assertRaisesRegex(ValueError, "destination is a symlink"):
                publish_sessions(sessions, archive, "desktop")

    def test_publisher_rejects_conflicting_single_file_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.write_fixture(root / "first")
            second = self.write_fixture(root / "second")
            second.write_text(
                second.read_text(encoding="utf-8").replace('"session-1"', '"session-2"'),
                encoding="utf-8",
            )
            archive = root / "archive"
            publish_sessions(first, archive, "desktop")
            with self.assertRaisesRegex(ValueError, "different single-file session"):
                publish_sessions(second, archive, "desktop")

    def test_publisher_updates_a_single_file_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions")
            archive = root / "archive"
            publish_sessions(source, archive, "desktop")
            source.write_text(
                source.read_text(encoding="utf-8") + message("user", "A later request."),
                encoding="utf-8",
            )
            result = publish_sessions(source, archive, "desktop")
            self.assertEqual((result.copied, result.skipped), (1, 0))

    def test_publisher_replaces_multiply_linked_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            source = self.write_fixture(sessions)
            archive = root / "archive"
            initial = publish_sessions(sessions, archive, "desktop")
            destination = initial.destination / source.name
            alias = root / "archive-alias.jsonl"
            os.link(destination, alias)
            result = publish_sessions(sessions, archive, "desktop")
            self.assertEqual((result.copied, result.skipped), (1, 0))
            self.assertEqual(destination.stat().st_nlink, 1)
            self.assertEqual(alias.stat().st_nlink, 1)

    def test_shared_index_prefers_the_newest_duplicate_session_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            source = self.write_fixture(sessions / "2026" / "09")
            archive = root / "archive"
            publish_sessions(source, archive, "desktop")
            flat_copy = archive / "desktop" / source.name
            source.write_text(source.read_text(encoding="utf-8") + message("user", "A newer archived request."), encoding="utf-8")
            publish_sessions(sessions, archive, "desktop")
            os.utime(flat_copy, ns=(flat_copy.stat().st_atime_ns, flat_copy.stat().st_mtime_ns - 1))
            db = root / "index.sqlite"
            index_sessions(archive, db)
            with sqlite3.connect(db) as connection:
                self.assertEqual(
                    connection.execute("SELECT text FROM messages ORDER BY seq DESC LIMIT 1").fetchone()[0],
                    "A newer archived request.",
                )

    def test_changed_copy_reindexes_when_source_mtime_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            source = self.write_fixture(sessions)
            archive = root / "archive"
            publish_sessions(sessions, archive, "desktop")
            db = root / "index.sqlite"
            index_sessions(archive, db)
            original = source.stat()
            source.write_text(
                source.read_text(encoding="utf-8").replace("browser-only", "offline-tool"),
                encoding="utf-8",
            )
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
            publish_sessions(sessions, archive, "desktop")
            index_sessions(archive, db)
            with sqlite3.connect(db) as connection:
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM messages WHERE text LIKE '%offline-tool%'").fetchone()[0],
                    1,
                )

    def test_publisher_does_not_reuse_a_stale_content_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            source = self.write_fixture(sessions)
            archive = root / "archive"
            publish_sessions(sessions, archive, "desktop")
            publish_sessions(sessions, archive, "desktop")
            original = source.stat()
            source.write_text(
                source.read_text(encoding="utf-8").replace("browser-only", "offline-tool"),
                encoding="utf-8",
            )
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
            result = publish_sessions(sessions, archive, "desktop")
            self.assertEqual((result.copied, result.skipped), (1, 0))

    def test_duplicate_suppression_removes_the_stale_path_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = self.write_fixture(root / "sessions", "older.jsonl")
            newer = self.write_fixture(root / "sessions", "newer.jsonl")
            newer.write_text(newer.read_text(encoding="utf-8").replace('"session-1"', '"session-2"'), encoding="utf-8")
            os.utime(newer, ns=(newer.stat().st_atime_ns, newer.stat().st_mtime_ns + 1))
            db = root / "index.sqlite"
            index_sessions(root / "sessions", db)

            older.write_text(older.read_text(encoding="utf-8").replace('"session-1"', '"session-2"'), encoding="utf-8")
            os.utime(older, ns=(older.stat().st_atime_ns, newer.stat().st_mtime_ns - 1))
            index_sessions(root / "sessions", db)

            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT session_id FROM sessions").fetchone()[0], "session-2")

    def test_changed_duplicate_winner_restores_the_previous_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = self.write_fixture(root / "sessions", "a.jsonl")
            newer = self.write_fixture(root / "sessions", "z.jsonl")
            os.utime(newer, ns=(newer.stat().st_atime_ns, newer.stat().st_mtime_ns + 1))
            db = root / "index.sqlite"
            index_sessions(root / "sessions", db)

            newer.write_text(newer.read_text(encoding="utf-8").replace('"session-1"', '"session-2"'), encoding="utf-8")
            index_sessions(root / "sessions", db)

            with sqlite3.connect(db) as connection:
                self.assertEqual(
                    {row[0] for row in connection.execute("SELECT session_id FROM sessions")},
                    {"session-1", "session-2"},
                )

    def test_limit_excludes_later_duplicate_reconciliation_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            winner = self.write_fixture(root / "sessions", "a.jsonl")
            remaining_copy = self.write_fixture(root / "sessions", "z.jsonl")
            os.utime(winner, ns=(winner.stat().st_atime_ns, winner.stat().st_mtime_ns + 1))
            db = root / "index.sqlite"
            index_sessions(root / "sessions", db)

            winner.write_text(winner.read_text(encoding="utf-8").replace('"session-1"', '"session-2"'), encoding="utf-8")
            index_sessions(root / "sessions", db, limit=1)

            with sqlite3.connect(db) as connection:
                self.assertEqual(
                    {row[0] for row in connection.execute("SELECT session_id FROM sessions")},
                    {"session-1", "session-2"},
                )
            self.assertTrue(remaining_copy.exists())

    def test_limit_preserves_a_deleted_duplicate_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.write_fixture(root / "sessions", "a.jsonl")
            deleted_winner = self.write_fixture(root / "sessions", "b.jsonl")
            last = self.write_fixture(root / "sessions", "z.jsonl")
            os.utime(
                deleted_winner,
                ns=(
                    deleted_winner.stat().st_atime_ns,
                    max(first.stat().st_mtime_ns, last.stat().st_mtime_ns) + 1,
                ),
            )
            db = root / "index.sqlite"
            index_sessions(root / "sessions", db)
            deleted_winner.unlink()
            index_sessions(root / "sessions", db, limit=1)

            with sqlite3.connect(db) as connection:
                self.assertEqual(
                    connection.execute("SELECT source_path FROM sessions").fetchone()[0],
                    str(deleted_winner.resolve()),
                )

    def test_unchanged_index_does_not_reparse_sessions_for_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_fixture(root / "sessions")
            db = root / "index.sqlite"
            index_sessions(root / "sessions", db)
            with patch.object(session_index, "parse_session_file") as parse:
                index_sessions(root / "sessions", db)
            parse.assert_not_called()

    def test_removed_duplicate_winner_restores_the_remaining_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = self.write_fixture(root / "sessions", "a.jsonl")
            newer = self.write_fixture(root / "sessions", "z.jsonl")
            os.utime(newer, ns=(newer.stat().st_atime_ns, newer.stat().st_mtime_ns + 1))
            db = root / "index.sqlite"
            index_sessions(root / "sessions", db)
            newer.unlink()
            index_sessions(root / "sessions", db)

            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT source_path FROM sessions").fetchone()[0], str(older.resolve()))

    def test_malformed_duplicate_winner_restores_the_remaining_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = self.write_fixture(root / "sessions", "a.jsonl")
            newer = self.write_fixture(root / "sessions", "z.jsonl")
            os.utime(newer, ns=(newer.stat().st_atime_ns, newer.stat().st_mtime_ns + 1))
            db = root / "index.sqlite"
            index_sessions(root / "sessions", db)
            newer.write_text("not valid session metadata\n", encoding="utf-8")
            index_sessions(root / "sessions", db)

            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT source_path FROM sessions").fetchone()[0], str(older.resolve()))

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

    def test_shared_index_removes_rows_from_a_prior_local_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = self.write_fixture(root / "local")
            shared = self.write_fixture(root / "shared" / "desktop")
            shared.write_text(shared.read_text(encoding="utf-8").replace('"session-1"', '"shared-session"'), encoding="utf-8")
            db = root / "index.sqlite"
            index_sessions(local.parent, db)
            with patch.dict(
                "os.environ",
                {"CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT": str(root / "shared")},
                clear=False,
            ):
                codex_index.main(["--shared", "--db", str(db)])
            with sqlite3.connect(db) as connection:
                self.assertEqual(
                    {row[0] for row in connection.execute("SELECT session_id FROM sessions")},
                    {"shared-session"},
                )

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
