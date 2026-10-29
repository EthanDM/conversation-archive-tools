from __future__ import annotations

import io
import json
import errno
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid

import zstandard
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_DIR))

from conversation_archive.codex.context_candidates import build_candidates, load_rules
from conversation_archive.codex import index as codex_index
from conversation_archive.codex import publish as codex_publish
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
    def setUp(self) -> None:
        self.installation_directory = tempfile.TemporaryDirectory()
        self.installation_patch = patch.object(
            codex_publish,
            "INSTALLATION_ID_PATH",
            Path(self.installation_directory.name) / "installation-id",
        )
        self.installation_patch.start()

    def tearDown(self) -> None:
        self.installation_patch.stop()
        self.installation_directory.cleanup()

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

    def test_compressed_transition_preserves_searchable_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions")
            db = root / "index.sqlite"
            index_sessions(source.parent, db)
            compressed = source.with_name(source.name + ".zst")
            compressed.write_bytes(zstandard.ZstdCompressor().compress(source.read_bytes()))
            source.unlink()
            index_sessions(compressed.parent, db)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT source_path FROM sessions").fetchone()[0], str(compressed.resolve()))
                self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'text:browser'").fetchone()[0], 1)
            self.assertEqual(index_sessions(compressed.parent, db), (0, 1, 0))

    def test_corrupt_compressed_replacement_does_not_remove_indexed_history(self) -> None:
        for invalid in (b"not a zstd frame", zstandard.ZstdCompressor().compress(b"valid text")[:-1],
                        zstandard.ZstdCompressor().compress(b"no session metadata\n")):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self.write_fixture(root / "sessions")
                db = root / "index.sqlite"
                index_sessions(source.parent, db)
                source.with_name(source.name + ".zst").write_bytes(invalid)
                source.unlink()
                with self.assertRaises(ValueError):
                    index_sessions(root / "sessions", db)
                with sqlite3.connect(db) as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 3)

    def test_reset_failure_preserves_existing_index_and_success_rebuilds_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions")
            db = root / "index.sqlite"
            index_sessions(source.parent, db)
            broken = source.parent / "broken.jsonl.zst"
            broken.write_bytes(b"invalid zstandard data")
            with self.assertRaises(ValueError):
                index_sessions(source.parent, db, reset=True)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'text:browser'").fetchone()[0], 1)
            broken.unlink()
            source.write_text(event("session_meta", {"id": "new-session"}) + message("user", "rebuilt text"))
            index_sessions(source.parent, db, reset=True)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT session_id FROM sessions").fetchone()[0], "new-session")
                self.assertEqual(connection.execute("SELECT text FROM messages").fetchone()[0], "rebuilt text")
                self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'text:browser'").fetchone()[0], 0)

    def test_reset_recovers_corrupt_or_incompatible_database_only_after_success(self) -> None:
        for invalid_database in ("corrupt", "incompatible"):
            with self.subTest(invalid_database=invalid_database), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self.write_fixture(root / "sessions")
                db = root / "index.sqlite"
                if invalid_database == "corrupt":
                    db.write_bytes(b"not a sqlite database")
                else:
                    with sqlite3.connect(db) as connection:
                        connection.execute("CREATE TABLE sessions (wrong_column TEXT)")
                before = db.read_bytes()
                broken = source.parent / "broken.jsonl.zst"
                broken.write_bytes(b"invalid zstandard data")
                with self.assertRaises(ValueError):
                    index_sessions(source.parent, db, reset=True)
                self.assertEqual(db.read_bytes(), before)
                self.assertEqual(list(root.glob(".index.sqlite.*.tmp")), [])
                broken.unlink()
                self.assertEqual(index_sessions(source.parent, db, reset=True), (1, 0, 3))
                with sqlite3.connect(db) as connection:
                    self.assertEqual(connection.execute("SELECT session_id FROM sessions").fetchone()[0], "session-1")
                    self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 3)

    def test_reset_does_not_modify_incompatible_index_when_input_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "index.sqlite"
            with sqlite3.connect(db) as connection:
                connection.executescript("""
                    CREATE TABLE sessions(session_id TEXT PRIMARY KEY, source_path TEXT, file_size INTEGER,
                                          file_mtime_ns INTEGER, last_time_iso TEXT);
                    CREATE TABLE messages(session_id TEXT, seq INTEGER, role TEXT, create_time_iso TEXT, text TEXT);
                    CREATE TABLE index_meta(key TEXT, value TEXT);
                """)
            before = db.read_bytes()
            source = root / "broken.jsonl.zst"
            source.write_bytes(b"not zstandard")
            with self.assertRaises(ValueError):
                index_sessions(source, db, reset=True)
            self.assertEqual(db.read_bytes(), before)

    def test_reset_holds_sqlite_writer_lock_through_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions")
            db = root / "index.sqlite"
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE old_data(value TEXT)")
            replace = os.replace
            writer_blocked = []

            def check_writer_then_replace(source_path: object, destination_path: object) -> None:
                try:
                    with sqlite3.connect(db, timeout=0) as writer:
                        writer.execute("INSERT INTO old_data VALUES ('must not commit')")
                except sqlite3.OperationalError as error:
                    writer_blocked.append(str(error))
                writer_code = (
                    "import sqlite3, sys; "
                    "c = sqlite3.connect(sys.argv[1], timeout=0); "
                    "c.execute(\"INSERT INTO old_data VALUES ('other process')\"); c.commit()"
                )
                other = subprocess.run([sys.executable, "-c", writer_code, str(db)], capture_output=True, text=True)
                self.assertNotEqual(other.returncode, 0)
                self.assertIn("locked", other.stderr)
                replace(source_path, destination_path)

            with patch.object(session_index.os, "replace", side_effect=check_writer_then_replace):
                index_sessions(source.parent, db, reset=True)
            self.assertEqual(len(writer_blocked), 1)
            self.assertIn("locked", writer_blocked[0])
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)

    def test_reset_does_not_replace_database_changed_during_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions")
            db = root / "index.sqlite"
            db.write_bytes(b"not a sqlite database")
            refresh = session_index._refresh_index

            def refresh_and_change_database(*args: object, **kwargs: object):
                result = refresh(*args, **kwargs)
                db.write_bytes(b"changed while rebuilding")
                return result

            with patch.object(session_index, "_refresh_index", side_effect=refresh_and_change_database):
                with self.assertRaisesRegex(ValueError, "changed during recovery"):
                    index_sessions(source.parent, db, reset=True)
            self.assertEqual(db.read_bytes(), b"changed while rebuilding")

    def test_reset_refuses_to_replace_unreadable_database_with_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions")
            db = root / "index.sqlite"
            db.write_bytes(b"not a sqlite database")
            sidecar = root / "index.sqlite-wal"
            sidecar.write_bytes(b"uncheckpointed data")
            with self.assertRaisesRegex(ValueError, "SQLite sidecars"):
                index_sessions(source.parent, db, reset=True)
            self.assertEqual(db.read_bytes(), b"not a sqlite database")
            self.assertEqual(sidecar.read_bytes(), b"uncheckpointed data")

    def test_reset_refuses_missing_database_with_live_wal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions")
            db = root / "index.sqlite"
            connection = sqlite3.connect(db)
            try:
                self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                connection.execute("CREATE TABLE old_data(value TEXT)")
                connection.execute("INSERT INTO old_data VALUES ('old history')")
                connection.commit()
                wal = root / "index.sqlite-wal"
                self.assertTrue(wal.exists())
                original_wal = wal.read_bytes()
                db.unlink()
                with self.assertRaisesRegex(ValueError, "SQLite sidecars"):
                    index_sessions(source.parent, db, reset=True)
                self.assertFalse(db.exists())
                self.assertEqual(wal.read_bytes(), original_wal)
                self.assertEqual(connection.execute("SELECT value FROM old_data").fetchone()[0], "old history")
            finally:
                connection.close()

    def test_reset_rejects_sidecars_arriving_during_missing_database_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions")
            db = root / "index.sqlite"
            wal = root / "index.sqlite-wal"
            link = os.link

            def link_after_sidecar_arrives(source_path: object, destination_path: object) -> None:
                wal.write_bytes(b"restored sidecar")
                link(source_path, destination_path)
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    with sqlite3.connect(db, timeout=0) as reader:
                        reader.execute("SELECT count(*) FROM sessions").fetchone()

            with patch.object(session_index.os, "link", side_effect=link_after_sidecar_arrives):
                with self.assertRaisesRegex(ValueError, "SQLite sidecars"):
                    index_sessions(source.parent, db, reset=True)
            self.assertFalse(db.exists())
            self.assertEqual(wal.read_bytes(), b"restored sidecar")
            self.assertEqual(list(root.glob(".index.sqlite.*.tmp")), [])

    def test_missing_index_remains_locked_after_final_sidecar_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions")
            db = root / "index.sqlite"
            check = session_index._check_index_sidecars
            attempts = []

            def check_then_try_writer(path: Path) -> None:
                check(path)
                attempts.append(1)
                if len(attempts) == 2:
                    writer_code = (
                        "import sqlite3, sys; "
                        "c = sqlite3.connect(sys.argv[1], timeout=0); "
                        "c.execute(\"DELETE FROM sessions\"); c.commit()"
                    )
                    writer = subprocess.run([sys.executable, "-c", writer_code, str(db)], capture_output=True, text=True)
                    self.assertNotEqual(writer.returncode, 0)
                    self.assertIn("locked", writer.stderr)

            with patch.object(session_index, "_check_index_sidecars", side_effect=check_then_try_writer):
                index_sessions(source.parent, db, reset=True)
            self.assertEqual(len(attempts), 2)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)

    def test_invalid_same_path_compressed_session_preserves_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions")
            compressed = source.with_name(source.name + ".zst")
            compressed.write_bytes(zstandard.ZstdCompressor().compress(source.read_bytes()))
            source.unlink()
            db = root / "index.sqlite"
            index_sessions(compressed.parent, db)
            compressed.write_bytes(zstandard.ZstdCompressor().compress(b"no session metadata\n"))
            with self.assertRaisesRegex(ValueError, "no session ID"):
                index_sessions(compressed.parent, db)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'text:browser'").fetchone()[0], 1)

    def test_compressed_guardian_session_remains_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "guardian.jsonl.zst"
            content = event("session_meta", {"thread_source": "guardian_review"})
            source.write_bytes(zstandard.ZstdCompressor().compress(content.encode()))
            self.assertIsNone(parse_session_file(source))
            self.assertEqual(index_sessions(root, root / "index.sqlite"), (0, 0, 0))

    def test_publisher_copies_compressed_files_and_shared_index_reads_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions" / "2026")
            compressed = source.with_name(source.name + ".zst")
            compressed.write_bytes(zstandard.ZstdCompressor().compress(source.read_bytes()))
            source.unlink()
            archive = root / "archive"
            result = publish_sessions(root / "sessions", archive, "desktop")
            self.assertEqual(result.copied, 1)
            self.assertEqual((result.destination / "2026" / compressed.name).read_bytes(), compressed.read_bytes())
            self.assertEqual(publish_sessions(root / "sessions", archive, "desktop").skipped, 1)
            db = root / "index.sqlite"
            self.assertEqual(index_sessions(archive, db), (1, 0, 3))
            self.assertEqual(publish_sessions(compressed, archive, "desktop").copied, 1)

    def test_plain_and_compressed_duplicates_use_newer_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root / "sessions")
            compressed = source.with_name(source.name + ".zst")
            compressed.write_bytes(zstandard.ZstdCompressor().compress(source.read_bytes() + message("user", "newer compressed text").encode()))
            os.utime(source, ns=(1_000_000_000, 1_000_000_000))
            os.utime(compressed, ns=(2_000_000_000, 2_000_000_000))
            db = root / "index.sqlite"
            index_sessions(source.parent, db)
            index_sessions(source.parent, db)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
                self.assertIn("newer compressed text", [r[0] for r in connection.execute("SELECT text FROM messages")])

    def test_concatenated_compressed_frames_and_utf8_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.write_fixture(root)
            text = source.read_bytes() + message("user", "café").replace("\\u00e9", "é").encode()
            split = text.index("é".encode()) + 1
            compressor = zstandard.ZstdCompressor()
            compressed = source.with_name(source.name + ".zst")
            compressed.write_bytes(compressor.compress(text[:split]) + compressor.compress(text[split:]))
            self.assertEqual(parse_session_file(compressed).messages[-1].text, "café")

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

    def test_publisher_validates_single_file_input_before_claiming(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "session.txt"
            source.write_text("not jsonl", encoding="utf-8")
            archive = root / "archive"

            with self.assertRaisesRegex(ValueError, "not a JSONL file"):
                publish_sessions(source, archive, "desktop", wait_for_claim_sync=True)

            self.assertFalse((archive / ".machines" / "desktop.json").exists())

    def test_atomic_file_creation_preserves_an_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservation.json"
            path.write_text("existing\n", encoding="utf-8")

            self.assertFalse(codex_publish.atomically_create_file(path, "replacement\n"))
            self.assertEqual(path.read_text(encoding="utf-8"), "existing\n")

    def test_atomic_file_creation_syncs_the_published_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservation.json"
            with patch.object(codex_publish, "fsync_directory") as fsync_directory:
                self.assertTrue(codex_publish.atomically_create_file(path, "contents\n"))
            fsync_directory.assert_called_once_with(path.parent)

    def test_atomic_file_creation_requires_hard_link_support(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservation.json"
            with patch.object(codex_publish.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
                with self.assertRaisesRegex(ValueError, "require hard-link support"):
                    codex_publish.atomically_create_file(path, "contents\n")
            self.assertFalse(path.exists())

    def test_publisher_reserves_a_new_machine_id_and_reuses_its_own_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            self.write_fixture(sessions)
            archive = root / "archive"

            first = publish_sessions(sessions, archive, "desktop")
            reservation = archive / ".machines" / "desktop.json"
            first_installation = json.loads(reservation.read_text(encoding="utf-8"))["installation_id"]
            repeated = publish_sessions(sessions, archive, "desktop")

            self.assertEqual((first.copied, first.skipped), (1, 0))
            self.assertEqual((repeated.copied, repeated.skipped), (0, 1))
            self.assertEqual(json.loads(reservation.read_text(encoding="utf-8"))["installation_id"], first_installation)

    def test_publisher_waits_for_sync_after_a_new_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            self.write_fixture(sessions)
            archive = root / "archive"

            claimed = publish_sessions(sessions, archive, "desktop", wait_for_claim_sync=True)
            published = publish_sessions(
                sessions,
                archive,
                "desktop",
                wait_for_claim_sync=True,
                confirm_claim_sync=True,
            )

            self.assertTrue(claimed.claimed)
            self.assertEqual((claimed.copied, claimed.skipped), (0, 0))
            self.assertFalse(published.claimed)
            self.assertEqual((published.copied, published.skipped), (1, 0))

    def test_confirmation_does_not_publish_during_the_claiming_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            self.write_fixture(sessions)
            archive = root / "archive"

            claimed = publish_sessions(
                sessions,
                archive,
                "desktop",
                wait_for_claim_sync=True,
                confirm_claim_sync=True,
            )

            self.assertTrue(claimed.claimed)
            self.assertEqual((claimed.copied, claimed.skipped), (0, 0))
            self.assertFalse((archive / "desktop").exists())

    def test_concurrent_first_claim_observer_does_not_publish_with_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            self.write_fixture(sessions)

            with patch.object(codex_publish, "reserve_machine_id", return_value=(False, False)):
                claimed = publish_sessions(
                    sessions,
                    root / "archive",
                    "desktop",
                    wait_for_claim_sync=True,
                    confirm_claim_sync=True,
                )

            self.assertTrue(claimed.claimed)
            self.assertEqual((claimed.copied, claimed.skipped), (0, 0))

    def test_rotating_an_installation_id_generates_a_new_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "installation-id"
            original = codex_publish.installation_id(path)
            codex_publish.rotate_installation_id(path)

            self.assertNotEqual(codex_publish.installation_id(path), original)

    def test_cli_does_not_read_an_installation_id_without_rotation(self) -> None:
        target = Path(self.installation_directory.name) / "target"
        codex_publish.INSTALLATION_ID_PATH.symlink_to(target)
        with patch.object(
            codex_publish,
            "publish_sessions",
            return_value=codex_publish.PublishResult(0, 0, Path("/tmp/archive/desktop")),
        ):
            self.assertEqual(
                codex_publish.main(
                    ["--input", "/tmp/sessions", "--archive-root", "/tmp/archive", "--machine-id", "desktop"]
                ),
                0,
            )

    def test_rotation_retains_new_identity_after_a_reservation_is_created(self) -> None:
        original = codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH)
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive"

            def fail_after_claim(*args: object, **kwargs: object) -> codex_publish.PublishResult:
                installation = codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH)
                reservation = archive / ".machines" / "desktop.json"
                reservation.parent.mkdir(parents=True)
                reservation.write_text(json.dumps({"installation_id": installation}), encoding="utf-8")
                raise OSError("copy failed")

            with patch.object(codex_publish, "publish_sessions", side_effect=fail_after_claim):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    codex_publish.main(
                        [
                            "--input",
                            "/tmp/sessions",
                            "--archive-root",
                            str(archive),
                            "--machine-id",
                            "desktop",
                            "--rotate-installation-id",
                        ]
                    )

            self.assertNotEqual(codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH), original)

    def test_rotation_restores_the_old_identity_when_creating_the_new_one_fails(self) -> None:
        original = codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH)
        create_file = codex_publish.atomically_create_file

        def fail_new_identity(path: Path, contents: str) -> bool:
            if contents.strip() != original:
                raise OSError("new identity write failed")
            return create_file(path, contents)

        with patch.object(codex_publish, "atomically_create_file", side_effect=fail_new_identity):
            with self.assertRaisesRegex(OSError, "new identity write failed"):
                codex_publish.main(
                    [
                        "--input",
                        "/tmp/sessions",
                        "--archive-root",
                        "/tmp/archive",
                        "--machine-id",
                        "desktop",
                        "--rotate-installation-id",
                    ]
                )

        self.assertEqual(codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH), original)

    def test_rotation_restores_the_old_identity_when_reservation_probe_fails(self) -> None:
        original = codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH)
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive"

            def fail_with_corrupt_reservation(*args: object, **kwargs: object) -> codex_publish.PublishResult:
                reservation = archive / ".machines" / "desktop.json"
                reservation.parent.mkdir(parents=True)
                reservation.write_text("not json", encoding="utf-8")
                raise ValueError("reservation failed")

            with patch.object(codex_publish, "publish_sessions", side_effect=fail_with_corrupt_reservation):
                with self.assertRaisesRegex(ValueError, "reservation failed"):
                    codex_publish.main(
                        [
                            "--input",
                            "/tmp/sessions",
                            "--archive-root",
                            str(archive),
                            "--machine-id",
                            "desktop",
                            "--rotate-installation-id",
                        ]
                    )

        self.assertEqual(codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH), original)

    def test_rotation_does_not_restore_over_a_concurrent_new_identity(self) -> None:
        original = codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH)
        rotated = codex_publish.rotate_installation_id(codex_publish.INSTALLATION_ID_PATH)
        replacement = str(uuid.uuid4())
        codex_publish.INSTALLATION_ID_PATH.write_text(f"{replacement}\n", encoding="utf-8")

        self.assertFalse(
            codex_publish.should_restore_rotated_installation(
                codex_publish.INSTALLATION_ID_PATH,
                Path("/tmp/archive"),
                "desktop",
                rotated,
            )
        )
        self.assertNotEqual(replacement, original)

    def test_rotation_restores_the_old_identity_after_an_interrupt(self) -> None:
        original = codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH)
        with patch.object(codex_publish, "publish_sessions", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                codex_publish.main(
                    [
                        "--input",
                        "/tmp/sessions",
                        "--archive-root",
                        "/tmp/archive",
                        "--machine-id",
                        "desktop",
                        "--rotate-installation-id",
                    ]
                )

        self.assertEqual(codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH), original)

    def test_rotation_publishes_with_its_own_generated_identity(self) -> None:
        original = codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH)
        with patch.object(
            codex_publish,
            "publish_sessions",
            return_value=codex_publish.PublishResult(0, 0, Path("/tmp/archive/desktop")),
        ) as publish:
            self.assertEqual(
                codex_publish.main(
                    [
                        "--input",
                        "/tmp/sessions",
                        "--archive-root",
                        "/tmp/archive",
                        "--machine-id",
                        " desktop ",
                        "--rotate-installation-id",
                    ]
                ),
                0,
            )

        self.assertNotEqual(publish.call_args.kwargs["installation"], original)
        self.assertEqual(publish.call_args.args[2], "desktop")

    def test_rotation_locks_the_installation_id_until_publishing_finishes(self) -> None:
        with patch.object(
            codex_publish,
            "publish_sessions",
            return_value=codex_publish.PublishResult(0, 0, Path("/tmp/archive/desktop")),
        ), patch.object(codex_publish.fcntl, "flock") as flock:
            self.assertEqual(
                codex_publish.main(
                    [
                        "--input",
                        "/tmp/sessions",
                        "--archive-root",
                        "/tmp/archive",
                        "--machine-id",
                        "desktop",
                        "--rotate-installation-id",
                    ]
                ),
                0,
            )

        self.assertEqual(flock.call_args_list[0].args[1], codex_publish.fcntl.LOCK_EX)
        self.assertEqual(flock.call_args_list[-1].args[1], codex_publish.fcntl.LOCK_UN)

    def test_ordinary_publish_holds_a_shared_installation_id_lock(self) -> None:
        with patch.object(
            codex_publish,
            "publish_sessions",
            return_value=codex_publish.PublishResult(0, 0, Path("/tmp/archive/desktop")),
        ), patch.object(codex_publish.fcntl, "flock") as flock:
            self.assertEqual(
                codex_publish.main(
                    ["--input", "/tmp/sessions", "--archive-root", "/tmp/archive", "--machine-id", "desktop"]
                ),
                0,
            )

        self.assertEqual(flock.call_args_list[0].args[1], codex_publish.fcntl.LOCK_SH)
        self.assertEqual(flock.call_args_list[-1].args[1], codex_publish.fcntl.LOCK_UN)

    def test_rotation_lock_failure_does_not_restore_an_unchanged_identity(self) -> None:
        original = codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH)
        with patch.object(codex_publish, "installation_id_lock", side_effect=OSError("lock failed")):
            with self.assertRaisesRegex(OSError, "lock failed"):
                codex_publish.main(
                    [
                        "--input",
                        "/tmp/sessions",
                        "--archive-root",
                        "/tmp/archive",
                        "--machine-id",
                        "desktop",
                        "--rotate-installation-id",
                    ]
                )

        self.assertEqual(codex_publish.installation_id(codex_publish.INSTALLATION_ID_PATH), original)

    def test_rotation_restores_before_releasing_its_exclusive_lock(self) -> None:
        lock_modes: list[int] = []

        def restore_while_locked(*args: object) -> None:
            self.assertEqual(lock_modes[-1], codex_publish.fcntl.LOCK_EX)

        with patch.object(codex_publish, "publish_sessions", side_effect=OSError("publish failed")), patch.object(
            codex_publish.fcntl,
            "flock",
            side_effect=lambda _descriptor, mode: lock_modes.append(mode),
        ), patch.object(codex_publish, "restore_installation_id", side_effect=restore_while_locked):
            with self.assertRaisesRegex(OSError, "publish failed"):
                codex_publish.main(
                    [
                        "--input",
                        "/tmp/sessions",
                        "--archive-root",
                        "/tmp/archive",
                        "--machine-id",
                        "desktop",
                        "--rotate-installation-id",
                    ]
                )

        self.assertEqual(lock_modes[-1], codex_publish.fcntl.LOCK_UN)

    def test_publisher_rejects_a_machine_id_reserved_by_another_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            self.write_fixture(sessions)
            archive = root / "archive"
            publish_sessions(sessions, archive, "desktop", installation_id_path=root / "first-id")

            with self.assertRaisesRegex(ValueError, "already reserved"):
                publish_sessions(sessions, archive, "desktop", installation_id_path=root / "second-id")

    def test_publisher_requires_an_explicit_claim_for_a_populated_legacy_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            self.write_fixture(sessions)
            archive = root / "archive"
            self.write_fixture(archive / "desktop")

            with self.assertRaisesRegex(ValueError, "claim-existing-machine-id"):
                publish_sessions(sessions, archive, "desktop")
            self.assertFalse((archive / ".machines" / "desktop.json").exists())

            claimed = publish_sessions(sessions, archive, "desktop", claim_existing_machine_id=True)
            self.assertEqual((claimed.copied, claimed.skipped), (0, 1))
            self.assertTrue((archive / ".machines" / "desktop.json").is_file())

    def test_publisher_rejects_symlinked_or_corrupt_machine_reservations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            self.write_fixture(sessions)
            archive = root / "archive"
            reservation = archive / ".machines" / "desktop.json"
            reservation.parent.mkdir(parents=True)
            reservation.symlink_to(root / "outside.json")

            with self.assertRaisesRegex(ValueError, "reservation is a symlink"):
                publish_sessions(sessions, archive, "desktop")

            reservation.unlink()
            reservation.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reservation is corrupt"):
                publish_sessions(sessions, archive, "desktop")

    def test_publisher_cli_exposes_and_passes_the_legacy_claim_flag(self) -> None:
        self.assertIn("--claim-existing-machine-id", codex_publish.build_parser().format_help())
        with patch.object(
            codex_publish,
            "publish_sessions",
            return_value=codex_publish.PublishResult(0, 0, Path("/tmp/archive/desktop")),
        ) as publish:
            self.assertEqual(
                codex_publish.main(
                    [
                        "--input",
                        "/tmp/sessions",
                        "--archive-root",
                        "/tmp/archive",
                        "--machine-id",
                        "desktop",
                        "--claim-existing-machine-id",
                    ]
                ),
                0,
            )
        self.assertTrue(publish.call_args.kwargs["claim_existing_machine_id"])

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
                publish_sessions(sessions, archive, "desktop", claim_existing_machine_id=True)

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
