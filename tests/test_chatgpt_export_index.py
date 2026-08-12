from __future__ import annotations

import io
import sqlite3
import shutil
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_DIR))

from conversation_archive.chatgpt.audit import audit_export
from conversation_archive.chatgpt.handoff import export_handoff
from conversation_archive.chatgpt.index import index_export
from conversation_archive.chatgpt.search import search


FIXTURE_EXPORT = Path(__file__).parent / "fixtures" / "chatgpt_export"


class ChatGPTExportIndexTests(unittest.TestCase):
    def test_indexes_and_searches_a_synthetic_official_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "export.sqlite"
            index_export(
                FIXTURE_EXPORT,
                db_path,
                include_hidden=False,
                write_markdown_dir=None,
                limit=None,
                progress_every=0,
                reset=True,
                all_nodes=False,
                incremental=False,
            )
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM conversations").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 2)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    search(
                        db_path,
                        '"local archive"',
                        limit=5,
                        include_text=True,
                        context=1,
                        width=80,
                        snippet_lines=3,
                    ),
                    0,
                )
            self.assertIn("Fixture planning conversation", output.getvalue())
            self.assertIn("SQLite FTS index", output.getvalue())
            handoff_path = Path(temporary) / "handoff.md"
            self.assertEqual(export_handoff(db_path, ["fixture-conversation"], handoff_path, output_format="markdown", redactions=["SQLite"]), 1)
            self.assertIn("[REDACTED] FTS index", handoff_path.read_text(encoding="utf-8"))

    def test_audits_and_indexes_a_zip_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "export.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(FIXTURE_EXPORT / "conversations.json", "export/conversations.json")
            self.assertEqual(audit_export(archive_path)["conversations"], 1)
            db_path = root / "export.sqlite"
            index_export(archive_path, db_path, include_hidden=False, write_markdown_dir=None, limit=None, progress_every=0, reset=True, all_nodes=False, incremental=False)
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM conversations").fetchone()[0], 1)

    def test_indexes_a_sharded_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy(FIXTURE_EXPORT / "conversations.json", root / "conversations-001.json")
            db_path = root / "export.sqlite"
            index_export(root, db_path, include_hidden=False, write_markdown_dir=None, limit=None, progress_every=0, reset=True, all_nodes=False, incremental=False)
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM conversations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
