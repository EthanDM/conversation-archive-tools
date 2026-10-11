import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from conversation_archive.claude_code.session_index import index_sessions, connect_readonly
from conversation_archive.claude_code.search import main as search
from conversation_archive.claude_code.show import main as show


class ClaudeCodeSessionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'session.jsonl'
        self.db = self.root / 'index.sqlite'

    def event(self, uuid, role='user', content='searchable request', **extra):
        return dict(type=role, sessionId='session', uuid=uuid, parentUuid='parent',
                    timestamp='2026-10-11T12:00:00Z', cwd='/project',
                    message={'content': content}, **extra)

    def write(self, events):
        self.source.write_text('\n'.join(json.dumps(e) for e in events) + '\n{"partial":')

    def test_filter_provenance_and_retrieval(self):
        user = self.event('user')
        self.write([user, user, self.event('assistant', 'assistant', [
            {'type': 'thinking', 'thinking': 'secretthought'},
            {'type': 'tool_use', 'input': {'text': 'secrettool'}},
            {'type': 'text', 'text': 'visible answer'}], isSidechain=True),
            self.event('tool', content=[{'type': 'tool_result', 'content': 'secretresult'}]),
            self.event('meta', isMeta=True), self.event('system', promptSource='system'),
            {'type': 'custom-title', 'sessionId': 'session', 'customTitle': 'Named session'}])
        self.assertEqual(index_sessions(self.source, self.db), (1, 0, 2))
        with contextlib.closing(connect_readonly(self.db)) as connection:
            rows = connection.execute('SELECT * FROM messages ORDER BY seq').fetchall()
            self.assertEqual(rows[0]['uuid'], 'user')
            self.assertEqual(rows[1]['parent_uuid'], 'parent')
            self.assertEqual(rows[1]['is_sidechain'], 1)
            self.assertEqual(rows[1]['text'], 'visible answer')
            self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'secretthought OR secrettool OR secretresult'").fetchone()[0], 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(search(['searchable', '--db', str(self.db), '--no-refresh', '--context', '1']), 0)
            self.assertEqual(show(['--session', 'session', '--db', str(self.db)]), 0)
        self.assertIn('Named session', output.getvalue())
        self.assertIn('visible answer', output.getvalue())

    def test_compaction_summary_is_not_user_text(self):
        self.write([
            self.event('summary', content='generatedsummary', isCompactSummary=True),
            self.event('user', content='actual request', isCompactSummary=False),
            self.event('assistant', 'assistant', 'actual answer'),
        ])
        self.assertEqual(index_sessions(self.source, self.db), (1, 0, 2))
        with contextlib.closing(connect_readonly(self.db)) as connection:
            self.assertEqual(connection.execute('SELECT uuid FROM messages ORDER BY seq').fetchall()[0]['uuid'], 'user')
            session = connection.execute('SELECT title, user_message_count FROM sessions').fetchone()
            self.assertEqual(tuple(session), ('actual request', 1))
            self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'generatedsummary'").fetchone()[0], 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(show(['--session', 'session', '--db', str(self.db)]), 0)
        self.assertNotIn('generatedsummary', output.getvalue())
        self.assertIn('actual request', output.getvalue())

    def test_incremental_replacement_removes_old_fts(self):
        self.write([self.event('old', content='oldword')])
        self.assertEqual(index_sessions(self.source, self.db), (1, 0, 1))
        self.assertEqual(index_sessions(self.source, self.db), (0, 1, 1))
        self.write([self.event('new', content='newword'), self.event('next', content='another')])
        self.assertEqual(index_sessions(self.source, self.db), (1, 0, 2))
        with contextlib.closing(connect_readonly(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'oldword'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'text:newword'").fetchone()[0], 1)

    def test_other_database_refused_and_read_does_not_create(self):
        self.write([self.event('user')])
        with sqlite3.connect(self.db) as connection:
            connection.execute('CREATE TABLE unrelated (value TEXT)')
        with self.assertRaises(sqlite3.Error):
            index_sessions(self.source, self.db)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('unrelated',)])
        missing = self.root / 'missing.sqlite'
        with self.assertRaises(sqlite3.Error):
            connect_readonly(missing)
        self.assertFalse(missing.exists())

    def test_conflicting_session_preserves_previous(self):
        self.write([self.event('old')])
        index_sessions(self.source, self.db)
        other = self.event('new')
        other['sessionId'] = 'other'
        self.write([self.event('old'), other])
        with self.assertRaises(ValueError):
            index_sessions(self.source, self.db)
        with contextlib.closing(connect_readonly(self.db)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM messages').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
