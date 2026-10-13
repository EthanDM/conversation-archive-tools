"""Conversation-scoped FTS and exact passage/reference contracts on synthetic indexes."""
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from test_archive_retrieval import fixture
from conversation_archive import retrieval


class ScopedSearchTests(unittest.TestCase):
    def test_scope_text_only_matching_pagination_and_readonly_for_all_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            for provider in retrieval.PROVIDERS:
                with self.subTest(provider=provider):
                    path = fixture(Path(directory), provider)
                    foreign = 'conversation_uuid' if provider == 'claude' else 'conversation_id' if provider == 'chatgpt' else 'session_id'
                    with sqlite3.connect(path) as connection:
                        if provider == 'claude':
                            connection.execute("INSERT INTO conversations VALUES ('other','Other','2026-01-01',NULL,'fixture')")
                        elif provider == 'chatgpt':
                            connection.execute("INSERT INTO conversations(conversation_id,title) VALUES ('other','Other')")
                        else:
                            connection.execute("INSERT INTO sessions(session_id,source_path,title,file_size,file_mtime_ns,user_message_count,assistant_message_count) VALUES ('other','other','Other',0,0,0,0)")
                        # The same query in another conversation must never enter a scoped page.
                        if provider == 'claude':
                            connection.execute("INSERT INTO messages(id,uuid,conversation_uuid,seq,role,created_at,text) VALUES (3,'3','other',1,'user','2026-01-01T12:00:00Z','needle')")
                            connection.execute("INSERT INTO messages_fts(rowid,text,title) VALUES (3,'needle','Other')")
                        elif provider == 'chatgpt':
                            connection.execute("INSERT INTO messages(id,conversation_id,seq,node_id,role,create_time_iso,text) VALUES (3,'other',1,'3','user','2026-01-01T12:00:00Z','needle')")
                        else:
                            connection.execute(f"INSERT INTO messages(id,{foreign},seq,role,create_time_iso,text) VALUES (3,'other',1,'user','2026-01-01T12:00:00Z','needle')")
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    first = retrieval.search_within_conversation(provider, 'conv', 'needle', limit=1, path=path)
                    second = retrieval.search_within_conversation(provider, 'conv', 'needle', limit=1, offset=first['next_offset'], path=path)
                    self.assertTrue(first['has_more'])
                    self.assertFalse(second['has_more'])
                    items = first['items'] + second['items']
                    self.assertEqual({row['message_index_id'] for row in items}, {1, 2})
                    self.assertTrue(all(row['conversation_id'] == 'conv' and row['match_type'] == 'message' for row in items))
                    self.assertTrue(all(row['relevance_score'] is not None for row in items))
                    if provider == 'claude_code':
                        assistant = next(row for row in items if row['role'] == 'assistant')
                        self.assertEqual((assistant['parent_id'], assistant['is_sidechain']), ('1', 1))
                    titles = retrieval.search_within_conversation(provider, 'conv', '"Archive title"', path=path)
                    self.assertEqual(titles['items'], [])
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_role_inclusive_source_calendar_dates_and_missing_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = fixture(Path(directory), 'chatgpt')
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE messages SET create_time_iso='2026-01-01T23:30:00-07:00' WHERE id=1")
                connection.execute('UPDATE messages SET create_time_iso=NULL WHERE id=2')
            result = retrieval.search_within_conversation('chatgpt', 'conv', 'needle', role='user', start_date='2026-01-01', end_date='2026-01-01', path=path)
            self.assertEqual([row['message_index_id'] for row in result['items']], [1])
            self.assertEqual(retrieval.search_within_conversation('chatgpt', 'conv', 'needle', start_date='2026-01-02', path=path)['items'], [])
            self.assertEqual(len(retrieval.search_within_conversation('chatgpt', 'conv', 'needle', path=path)['items']), 2)

    def test_passage_is_exact_and_can_be_read_back_at_its_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = fixture(Path(directory), 'chatgpt')
            text = '\u0130' * 1500 + ' payment decision ' + 'x' * 2000
            with sqlite3.connect(path) as connection:
                connection.execute('UPDATE messages SET text=? WHERE id=1', (text,))
            result = retrieval.search_within_conversation('chatgpt', 'conv', 'payment AND decision', path=path)
            row = result['items'][0]
            self.assertEqual(row['text'], text[row['text_offset']:row['text_offset'] + 1000])
            self.assertIn('payment decision', row['text'])
            self.assertGreater(row['text_offset'], 0)
            self.assertEqual(row['next_text_offset'], row['text_offset'] + len(row['text']))
            context = retrieval.read_message_context('chatgpt', row['message_index_id'], before=0, after=0, text_offset=row['text_offset'], path=path)
            self.assertTrue(context['items'][0]['text'].startswith(row['text']))
            references = retrieval.search_within_conversation('chatgpt', 'conv', 'payment', include_text=False, path=path)
            ref = references['items'][0]
            self.assertFalse(references['include_text'])
            self.assertTrue({'message_index_id', 'conversation_id', 'role', 'timestamp', 'source', 'cwd', 'parent_id', 'relevance_score'} <= ref.keys())
            self.assertFalse({'text', 'text_length', 'text_offset', 'next_text_offset', 'truncated'} & ref.keys())

    def test_absence_no_matches_bad_fts_and_missing_index_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            path = fixture(Path(directory), 'codex')
            with sqlite3.connect(path) as connection:
                connection.execute("INSERT INTO sessions(session_id,source_path,title,file_size,file_mtime_ns,user_message_count,assistant_message_count) VALUES ('empty','empty','Empty',0,0,0,0)")
            for conversation, available in [('conv', True), ('empty', True), ('missing', False)]:
                result = retrieval.search_within_conversation('codex', conversation, 'absent', path=path)
                self.assertEqual(result['conversation_available'], available)
                self.assertEqual(result['items'], [])
                self.assertFalse(result['has_more'])
                self.assertIsNone(result['next_offset'])
            for conversation in ['conv', 'empty', 'missing']:
                with self.assertRaises(sqlite3.Error):
                    retrieval.search_within_conversation('codex', conversation, '"', path=path)
            with sqlite3.connect(path) as connection:
                connection.execute('DELETE FROM messages')
            with self.assertRaises(sqlite3.Error):
                retrieval.search_within_conversation('codex', 'empty', '"', path=path)
            missing = Path(directory) / 'missing.sqlite'
            with self.assertRaises(sqlite3.Error):
                retrieval.search_within_conversation('codex', 'conv', 'needle', path=missing)
            self.assertFalse(missing.exists())

    def test_invalid_inputs_are_rejected_before_index_access(self):
        options = dict(path=Path('/tmp/nonexistent-scoped-index.sqlite'))
        for override in [{'limit': 51}, {'offset': -1}, {'role': 'system'}, {'include_text': 1},
                         {'start_date': 'bad'}, {'start_date': '2026-01-02', 'end_date': '2026-01-01'}]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                retrieval.search_within_conversation('codex', 'conv', 'needle', **options, **override)
        for conversation in ['', 'x' * 201, 123]:
            with self.assertRaises(ValueError):
                retrieval.search_within_conversation('codex', conversation, 'needle', **options)


if __name__ == '__main__':
    unittest.main()
