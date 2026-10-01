from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from conversation_archive.claude import index, search, show


def conversation(uuid="c1"):
    return {"uuid": uuid, "name": "Planning orchard", "summary": "SUMMARY_SENTINEL", "chat_messages": [
        {"uuid": uuid + "m1", "sender": "human", "text": "LEGACY_SENTINEL", "content": [
            {"type": "text", "text": "orchard first"}, {"type": "thinking", "thinking": "THINKING_SENTINEL"},
            {"type": "text", "text": "second"}, {"type": "tool_use", "input": "TOOL_SENTINEL"},
            {"type": "tool_result", "content": "RESULT_SENTINEL"}, {"type": "token_budget", "text": "BUDGET_SENTINEL"},
            {"type": "unknown", "text": "UNKNOWN_SENTINEL"}],
         "attachments": [{"file_name": "diagram.png", "extracted_content": "ATTACHMENT_SENTINEL"}],
         "files": [{"file_name": "notes.pdf"}]},
        {"uuid": uuid + "m2", "sender": "assistant", "parent_message_uuid": uuid + "m1", "text": "orchard legacy reply"},
        {"uuid": uuid + "m3", "sender": "human", "parent_message_uuid": uuid + "m1", "content": [], "text": "EMPTY_CONTENT_SENTINEL"},
    ]}


class ClaudeExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "conversations.json"
        self.db = self.root / "claude.sqlite"
        self.write([conversation()])

    def write(self, data):
        self.input.write_text(json.dumps(data))

    def zip(self, path, data):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("conversations.json", json.dumps(data))
            archive.writestr("memories.json", "MEMORY_SENTINEL")

    def run_cli(self, command, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = command(args)
        return result, out.getvalue(), err.getvalue()

    def test_filtering_and_metadata(self):
        self.assertEqual(index.index_export(self.input, self.db), (1, 3))
        with contextlib.closing(index.open_readonly(self.db)) as db:
            rows = db.execute("SELECT * FROM messages ORDER BY seq").fetchall()
            self.assertEqual(rows[0]['text'], "orchard first\nsecond\n[Attachment reference: diagram.png]\n[Attachment reference: notes.pdf]")
            self.assertEqual(rows[0]['role'], 'user')
            self.assertEqual(rows[1]['parent_uuid'], 'c1m1')
            self.assertEqual(rows[1]['text'], 'orchard legacy reply')
            self.assertEqual(rows[2]['text'], '')
            for sentinel in ['LEGACY', 'THINKING', 'TOOL', 'RESULT', 'BUDGET', 'UNKNOWN', 'ATTACHMENT', 'SUMMARY', 'EMPTY_CONTENT']:
                self.assertEqual(db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?", (sentinel + '_SENTINEL',)).fetchone()[0], 0)
            self.assertIsNone(rows[0]['created_at'])

    def test_zip_directory_precedence_and_multipart(self):
        self.zip(self.root / 'conversations-000.zip', [conversation('z1')])
        self.zip(self.root / 'conversations-001.zip', [conversation('z2')])
        self.assertEqual(index.index_export(self.root, self.db), (1, 3))
        (self.root / 'extracted').mkdir()
        (self.root / 'extracted/conversations.json').write_text('[]')
        self.assertEqual(index.index_export(self.root, self.db), (0, 0))
        (self.root / 'extracted/conversations.json').unlink()
        self.input.unlink()
        self.assertEqual(index.index_export(self.root, self.db), (2, 6))
        self.assertEqual(index.index_export(self.root / 'conversations-000.zip', self.db), (1, 3))
        self.assertNotIn('MEMORY_SENTINEL', self.db.read_bytes().decode(errors='ignore'))

    def test_replacement_and_failure_preservation(self):
        index.index_export(self.input, self.db)
        index.index_export(self.input, self.db)
        with contextlib.closing(index.open_readonly(self.db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 3)
        for data in [[conversation(), conversation()], [dict(conversation(), chat_messages=[conversation()['chat_messages'][0]] * 2)], {}, [dict(conversation(), chat_messages='bad')], [dict(conversation(), uuid=None)]]:
            self.write(data)
            before = self.db.read_bytes()
            with self.assertRaises(ValueError):
                index.index_export(self.input, self.db)
            self.assertEqual(before, self.db.read_bytes())
        malformed = conversation()
        malformed['chat_messages'][0]['content'] = "not blocks"
        self.write([malformed])
        with self.assertRaises(ValueError):
            index.index_export(self.input, self.db)
        bad = self.root / 'bad.zip'
        bad.write_bytes(b'invalid zip')
        with self.assertRaises(zipfile.BadZipFile):
            index.index_export(bad, self.db)
        self.write([conversation('replacement')])
        index.index_export(self.input, self.db)
        with contextlib.closing(index.open_readonly(self.db)) as db:
            self.assertEqual(db.execute('SELECT uuid FROM conversations').fetchone()[0], 'replacement')
            self.assertEqual(db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'orchard'").fetchone()[0], 3)

    def test_other_provider_refused(self):
        for provider in ['chatgpt', 'codex']:
            db_path = self.root / (provider + '.sqlite')
            with sqlite3.connect(db_path) as db:
                db.execute('CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)')
                db.execute('INSERT INTO index_meta VALUES (?, ?)', ('provider', provider))
            before = db_path.read_bytes()
            with self.assertRaises(ValueError):
                index.index_export(self.input, db_path)
            self.assertEqual(before, db_path.read_bytes())
        with self.assertRaises(ValueError):
            index.index_export(self.input, self.input)

    def test_commands(self):
        index.index_export(self.input, self.db)
        before = self.db.read_bytes()
        args = ['--db', str(self.db)]
        code, out, _ = self.run_cli(search.main, ['orchard', '--role', 'assistant', *args])
        self.assertEqual(code, 0)
        self.assertIn('message=c1m2', out)
        self.assertNotIn('message=c1m1', out)
        code, out, _ = self.run_cli(search.main, ['"orchard first"', '--context', '1', *args])
        self.assertEqual(code, 0)
        self.assertIn('legacy reply', out)
        code, out, _ = self.run_cli(show.main, ['--title', 'orchard', '--list', *args])
        self.assertEqual(code, 0)
        self.assertIn('conv=c1', out)
        code, out, _ = self.run_cli(show.main, ['--conv', 'c1', '--max-chars', '5', *args])
        self.assertEqual(code, 0)
        self.assertIn('parent: c1m1', out)
        self.assertIn('orch…', out)
        self.assertNotIn('legacy reply', out)
        self.assertEqual(self.run_cli(show.main, ['--conv', 'absent', *args])[0], 1)
        self.assertEqual(self.run_cli(search.main, ['nomatch', *args])[0], 1)
        self.assertEqual(self.run_cli(search.main, ['"', *args])[0], 2)
        missing = self.root / 'missing.sqlite'
        self.assertEqual(self.run_cli(search.main, ['anything', '--db', str(missing)])[0], 2)
        self.assertEqual(self.run_cli(show.main, ['--conv', 'c1', '--db', str(missing)])[0], 2)
        self.assertFalse(missing.exists())
        self.assertEqual(before, self.db.read_bytes())

    def test_ranking(self):
        first = conversation('a')
        second = conversation('b')
        first['name'] = second['name'] = 'Neutral'
        first['chat_messages'] = [dict(first['chat_messages'][1], text='pear pear pear')]
        second['chat_messages'] = [dict(second['chat_messages'][1], text='pear unrelated words plus filler')]
        self.write([first, second])
        index.index_export(self.input, self.db)
        code, out, _ = self.run_cli(search.main, ['pear', '--limit', '1', '--db', str(self.db)])
        self.assertEqual(code, 0)
        self.assertIn('conv=a', out)
        self.assertNotIn('conv=b', out)

    def test_environment_and_cli_precedence(self):
        with patch.dict(os.environ, {'CLAUDE_EXPORT_INPUT': str(self.input), 'CLAUDE_EXPORT_DB': str(self.db)}):
            self.assertEqual(self.run_cli(index.main, [])[0], 0)
            alternate = self.root / 'alternate.sqlite'
            self.assertEqual(self.run_cli(index.main, ['--db', str(alternate)])[0], 0)
            other = self.root / 'other.json'
            other.write_text('[]')
            self.assertEqual(self.run_cli(index.main, ['--input', str(other)])[0], 0)
            with contextlib.closing(index.open_readonly(self.db)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM conversations').fetchone()[0], 0)

    def test_missing_titles_timestamps_and_invalid_json(self):
        record = conversation()
        record.pop('name')
        record['created_at'] = '2025-01-01T00:00:00Z'
        record['updated_at'] = '2025-01-02T00:00:00Z'
        record['chat_messages'][0]['created_at'] = record['created_at']
        self.write([record])
        index.index_export(self.input, self.db)
        with contextlib.closing(index.open_readonly(self.db)) as db:
            row = db.execute('SELECT * FROM conversations').fetchone()
            self.assertEqual(row['title'], 'Untitled conversation')
            self.assertEqual(row['updated_at'], record['updated_at'])
            self.assertEqual(db.execute('SELECT created_at FROM messages ORDER BY seq').fetchone()[0], record['created_at'])
            self.assertEqual(dict(db.execute('SELECT key, value FROM index_meta')), {'provider': 'claude', 'schema_version': '1'})
        before = self.db.read_bytes()
        self.input.write_text('{broken')
        self.assertEqual(self.run_cli(index.main, ['--input', str(self.input), '--db', str(self.db)])[0], 2)
        self.assertEqual(before, self.db.read_bytes())

    def test_transaction_rollback(self):
        index.index_export(self.input, self.db)
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TRIGGER reject_new BEFORE INSERT ON conversations BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        self.write([conversation('new')])
        with self.assertRaises(sqlite3.IntegrityError):
            index.index_export(self.input, self.db)
        with contextlib.closing(index.open_readonly(self.db)) as db:
            self.assertEqual(db.execute('SELECT uuid FROM conversations').fetchone()[0], 'c1')
            self.assertEqual(db.execute('SELECT count(*) FROM messages_fts').fetchone()[0], 3)


if __name__ == '__main__':
    unittest.main()
