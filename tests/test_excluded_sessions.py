"""Excluded sessions never reach a reader and leave the index on refresh; synthetic data only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from conversation_archive import retrieval
from conversation_archive.claude_code.session_index import index_sessions as index_claude_code
from conversation_archive.codex.session_index import index_sessions as index_codex
from conversation_archive.exclusions import excluded_session_ids, sql_condition

from test_archive_retrieval import fixture


def codex_session(path: Path, meta: dict, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"timestamp": "2026-11-01T12:00:00Z", "type": "session_meta", "payload": {"cwd": "/project", **meta}},
        {"timestamp": "2026-11-01T12:01:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}},
    ]
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def claude_code_session(path: Path, session_ids: list[str], text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [dict(type="user", sessionId=session_id, uuid=f"u{index}", parentUuid=None,
                   timestamp="2026-11-01T12:00:00Z", cwd="/project", message={"content": text})
              for index, session_id in enumerate(session_ids)]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


class ExcludedSessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.lists = self.root / "excluded-sessions"
        environment = patch.dict(os.environ, {"CONVERSATION_ARCHIVE_EXCLUDED_SESSIONS_DIR": str(self.lists)})
        environment.start()
        self.addCleanup(environment.stop)

    def exclude(self, *session_ids: str, name: str = "app.txt") -> None:
        self.lists.mkdir(exist_ok=True)
        (self.lists / name).write_text("# hidden from agents\n\n" + "\n".join(session_ids) + "\n", encoding="utf-8")

    def test_lists_merge_ignore_comments_and_fail_closed(self) -> None:
        self.assertEqual(excluded_session_ids(), frozenset())
        self.exclude("a-1")
        self.exclude("b_2", name="other.txt")
        (self.lists / "notes.md").write_text("not-a-list'\n", encoding="utf-8")
        self.assertEqual(excluded_session_ids(), frozenset({"a-1", "b_2"}))
        self.exclude("bad' OR 1=1 --", name="broken.txt")
        with self.assertRaisesRegex(ValueError, "broken.txt"):
            excluded_session_ids()

    def test_sql_condition_excludes_owned_subagent_streams(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.executescript("CREATE TABLE s(id TEXT); INSERT INTO s VALUES ('a'),('a:subagent:x'),('ab'),('b');")
        kept = connection.execute(f"SELECT id FROM s WHERE {sql_condition('id', frozenset({'a'}))} ORDER BY id").fetchall()
        self.assertEqual([row[0] for row in kept], ["ab", "b"])
        self.assertEqual(sql_condition("id", frozenset()), "1=1")

    def test_every_read_of_a_session_index_hides_listed_sessions(self) -> None:
        for provider in ("codex", "claude_code"):
            with self.subTest(provider=provider):
                path = fixture(self.root, provider)
                self.assertTrue(retrieval.search_conversations(provider, "needle", path=path)["items"])
                self.exclude("conv")
                self.assertEqual(retrieval.search_conversations(provider, "needle", path=path)["items"], [])
                self.assertEqual(retrieval.search_topic(provider, "needle", role=None, path=path)["items"], [])
                self.assertEqual(retrieval.list_conversations(provider, path=path)["items"], [])
                within = retrieval.search_within_conversation(provider, "conv", "needle", path=path)
                self.assertFalse(within["conversation_available"])
                self.assertEqual(within["items"], [])
                with self.assertRaisesRegex(ValueError, "Conversation not found"):
                    retrieval.read_conversation(provider, "conv", path=path)
                with self.assertRaisesRegex(ValueError, "Message not found"):
                    retrieval.read_message_context(provider, 1, path=path)
                (self.lists / "app.txt").unlink()

    def test_chat_exports_are_not_filtered(self) -> None:
        path = fixture(self.root, "chatgpt")
        self.exclude("conv")
        self.assertTrue(retrieval.search_conversations("chatgpt", "needle", path=path)["items"])

    def test_codex_refresh_drops_listed_sessions_and_their_subagents_and_forks(self) -> None:
        sessions, db = self.root / "codex", self.root / "codex.sqlite"
        codex_session(sessions / "hidden.jsonl", {"id": "hidden"}, "hiddenneedle")
        codex_session(sessions / "child.jsonl", {"id": "child", "parent_thread_id": "hidden",
                                                  "source": {"subagent": {"other": "worker"}}}, "childneedle")
        codex_session(sessions / "fork.jsonl", {"id": "fork", "forked_from_id": "hidden"}, "forkneedle")
        codex_session(sessions / "visible.jsonl", {"id": "visible"}, "visibleneedle")
        index_codex(sessions, db)
        ids = lambda: sorted(row[0] for row in sqlite3.connect(db).execute("SELECT session_id FROM sessions"))
        self.assertEqual(ids(), ["child", "fork", "hidden", "visible"])
        # The files are unchanged, so only the changed list can force them to be re-read.
        self.exclude("hidden")
        index_codex(sessions, db)
        self.assertEqual(ids(), ["visible"])
        (self.lists / "app.txt").unlink()
        index_codex(sessions, db)
        self.assertEqual(ids(), ["child", "fork", "hidden", "visible"])

    def test_limited_codex_refresh_does_not_record_the_new_list(self) -> None:
        sessions, db = self.root / "codex", self.root / "codex.sqlite"
        codex_session(sessions / "a-hidden.jsonl", {"id": "hidden"}, "hiddenneedle")
        codex_session(sessions / "b-fork.jsonl", {"id": "fork", "forked_from_id": "hidden"}, "forkneedle")
        index_codex(sessions, db)
        self.exclude("hidden")
        index_codex(sessions, db, limit=1)
        index_codex(sessions, db)
        ids = sorted(row[0] for row in sqlite3.connect(db).execute("SELECT session_id FROM sessions"))
        self.assertEqual(ids, [])

    def test_claude_code_refresh_drops_listed_sessions_forks_and_subagent_streams(self) -> None:
        projects, db = self.root / "projects", self.root / "claude_code.sqlite"
        claude_code_session(projects / "p" / "hidden.jsonl", ["hidden"], "hiddenneedle")
        claude_code_session(projects / "p" / "hidden" / "subagents" / "agent-1.jsonl", ["hidden"], "agentneedle")
        claude_code_session(projects / "p" / "fork.jsonl", ["hidden", "fork"], "forkneedle")
        claude_code_session(projects / "p" / "visible.jsonl", ["visible"], "visibleneedle")
        index_claude_code(projects, db)
        ids = lambda: sorted(row[0] for row in sqlite3.connect(db).execute("SELECT session_id FROM sessions"))
        self.assertEqual(ids(), ["fork", "hidden", "hidden:subagent:agent-1", "visible"])
        self.exclude("hidden")
        index_claude_code(projects, db)
        self.assertEqual(ids(), ["visible"])


if __name__ == "__main__":
    unittest.main()
