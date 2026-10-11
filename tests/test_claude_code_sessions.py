import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from conversation_archive.claude_code.paths import default_input_path
from conversation_archive.claude_code.session_index import index_sessions, connect_readonly
from conversation_archive.codex.session_index import SCHEMA_SQL
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
        self.write_path(self.source, events)

    def write_path(self, path, events):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(e) for e in events) + '\n{"partial":')

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
            self.assertEqual(search(['searchable', '--db', str(self.db), '--context', '1']), 0)
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

    def test_task_notifications_are_not_conversation_text(self):
        self.write([
            self.event('notification', content='machinegeneratedstatus',
                       origin={'kind': 'task-notification'}),
            self.event('assistant', 'assistant', 'visible answer'),
        ])
        self.assertEqual(index_sessions(self.source, self.db), (1, 0, 1))
        with contextlib.closing(connect_readonly(self.db)) as connection:
            self.assertEqual(connection.execute('SELECT title FROM sessions').fetchone()['title'],
                             'Claude Code session session')
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'machinegeneratedstatus'"
            ).fetchone()[0], 0)

    def test_external_channel_messages_are_indexed_as_human_prompts(self):
        self.write([
            self.event('channel', content='message from Discord', isMeta=True,
                       origin={'kind': 'channel'}, userType='external'),
            self.event('internal-meta', content='internal metadata', isMeta=True),
            self.event('assistant', 'assistant', 'channel answer'),
        ])
        self.assertEqual(index_sessions(self.source, self.db), (1, 0, 2))
        with contextlib.closing(connect_readonly(self.db)) as connection:
            session = connection.execute(
                'SELECT title, user_message_count FROM sessions'
            ).fetchone()
            self.assertEqual(tuple(session), ('message from Discord', 1))
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'text:Discord'"
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'internal'"
            ).fetchone()[0], 0)

    def test_filtered_empty_transcript_removes_previous_index(self):
        for incremental in (True, False):
            with self.subTest(incremental=incremental):
                db = self.root / f'filtered-{incremental}.sqlite'
                self.write([self.event('old', content='oldsearchterm')])
                self.assertEqual(index_sessions(self.source, db), (1, 0, 1))
                notification = self.event('notification', content='machine status',
                                          origin={'kind': 'task-notification'})
                self.source.write_text(json.dumps(notification) + '\n')
                self.assertEqual(index_sessions(self.source, db, incremental=incremental),
                                 (0, 0, 0))
                with contextlib.closing(connect_readonly(db)) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
                    self.assertEqual(connection.execute(
                        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'oldsearchterm'"
                    ).fetchone()[0], 0)

    def test_incomplete_filtered_transcript_preserves_previous_index(self):
        self.write([self.event('old', content='oldsearchterm')])
        self.assertEqual(index_sessions(self.source, self.db), (1, 0, 1))
        notification = self.event('notification', content='machine status',
                                  origin={'kind': 'task-notification'})
        self.source.write_text(json.dumps(notification) + '\n{"partial":')
        self.assertEqual(index_sessions(self.source, self.db, incremental=False), (0, 0, 1))
        with contextlib.closing(connect_readonly(self.db)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'oldsearchterm'"
            ).fetchone()[0], 1)

    def test_subagent_transcripts_use_distinct_stream_ids(self):
        source = self.root / 'sessions'
        self.write_path(source / 'session.jsonl', [self.event('main')])
        self.write_path(source / 'session' / 'subagents' / 'agent-worker.jsonl',
                        [self.event('worker', content='delegated answer')])
        self.write_path(source / 'session' / 'subagents' / 'workflows' / 'wf_example' /
                        'agent-worker.jsonl',
                        [self.event('workflow', content='workflow answer')])
        self.assertEqual(index_sessions(source, self.db), (3, 0, 3))
        with contextlib.closing(connect_readonly(self.db)) as connection:
            session_ids = {row['session_id'] for row in connection.execute(
                'SELECT session_id FROM sessions'
            )}
        self.assertEqual(session_ids, {
            'session',
            'session:subagent:agent-worker',
            'session:subagent:workflows:wf_example:agent-worker',
        })

    def test_forked_transcript_uses_child_session_id(self):
        source = self.root / 'child.jsonl'
        ancestor = self.event('ancestor', content='copied context')
        ancestor['sessionId'] = 'parent'
        child = self.event('child', content='continued request')
        child['sessionId'] = 'child'
        self.write_path(source, [ancestor, child])
        self.assertEqual(index_sessions(source, self.db), (1, 0, 2))
        with contextlib.closing(connect_readonly(self.db)) as connection:
            self.assertEqual(connection.execute('SELECT session_id FROM sessions').fetchone()['session_id'],
                             'child')

    def test_incremental_replacement_removes_old_fts(self):
        self.write([self.event('old', content='oldword')])
        self.assertEqual(index_sessions(self.source, self.db), (1, 0, 1))
        self.assertEqual(index_sessions(self.source, self.db), (0, 1, 1))
        self.write([self.event('new', content='newword'), self.event('next', content='another')])
        self.assertEqual(index_sessions(self.source, self.db), (1, 0, 2))
        with contextlib.closing(connect_readonly(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'oldword'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'text:newword'").fetchone()[0], 1)

    def test_invalid_fts_query_is_reported_without_traceback(self):
        self.write([self.event('user', content='searchable')])
        self.assertEqual(index_sessions(self.source, self.db), (1, 0, 1))
        error_output = io.StringIO()
        with contextlib.redirect_stderr(error_output):
            self.assertEqual(search(['"', '--db', str(self.db)]), 2)
        self.assertIn('Claude Code search failed: unterminated string',
                      error_output.getvalue())

    def test_input_default_honors_claude_config_dir(self):
        with patch.dict(os.environ, {
            'CLAUDE_CODE_SESSION_INPUT': '/explicit/sessions',
            'CLAUDE_CONFIG_DIR': '/custom/config',
        }, clear=True):
            self.assertEqual(default_input_path(), '/explicit/sessions')
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': '/custom/config'}, clear=True):
            self.assertEqual(default_input_path(), '/custom/config/projects')
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'home',
                                                                  return_value=Path('/tmp/generic-home')):
            self.assertEqual(default_input_path(), '/tmp/generic-home/.claude/projects')

    def test_migrates_legacy_database_and_removes_old_title_terms(self):
        with sqlite3.connect(self.db) as connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('ALTER TABLE messages ADD COLUMN uuid TEXT')
            connection.execute('ALTER TABLE messages ADD COLUMN parent_uuid TEXT')
            connection.execute('ALTER TABLE messages ADD COLUMN is_sidechain INTEGER NOT NULL DEFAULT 0')
            connection.executemany('INSERT INTO index_meta VALUES (?, ?)',
                                   [('provider', 'claude_code'), ('schema_version', '1')])
            connection.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               ('session', str(self.source.resolve()), '/project', 'OldUniqueTitle', None, None,
                                1, 1, 1, 0))
            connection.execute(
                'INSERT INTO messages (session_id, seq, role, create_time_iso, text, uuid) VALUES (?, ?, ?, ?, ?, ?)',
                ('session', 1, 'user', None, 'stable body', 'old'),
            )
            connection.execute("DELETE FROM sessions WHERE session_id='session'")
            connection.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               ('session', str(self.source.resolve()), '/project', 'CurrentUniqueTitle',
                                None, None, 1, 1, 1, 0))
            connection.execute(
                'INSERT INTO messages (session_id, seq, role, create_time_iso, text, uuid) VALUES (?, ?, ?, ?, ?, ?)',
                ('session', 1, 'user', None, 'stable body', 'current'),
            )
        self.write([
            {'type': 'custom-title', 'sessionId': 'session', 'customTitle': 'NewUniqueTitle'},
            self.event('new', content='stable body'),
        ])
        self.assertEqual(index_sessions(self.source, self.db), (1, 0, 1))
        with contextlib.closing(connect_readonly(self.db)) as connection:
            self.assertEqual(dict(connection.execute('SELECT key, value FROM index_meta'))['schema_version'], '2')
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'title:OldUniqueTitle'"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'title:CurrentUniqueTitle'"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'title:NewUniqueTitle'"
            ).fetchone()[0], 1)

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

    def test_duplicate_top_level_session_preserves_previous(self):
        source = self.root / 'sessions'
        self.write_path(source / 'session.jsonl', [self.event('old')])
        index_sessions(source, self.db)
        self.write_path(source / 'duplicate.jsonl', [self.event('new')])
        with self.assertRaises(ValueError):
            index_sessions(source, self.db)
        with contextlib.closing(connect_readonly(self.db)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM messages').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
