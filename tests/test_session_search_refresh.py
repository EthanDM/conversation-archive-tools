"""Exercise search refresh against synthetic sessions for both providers."""
import contextlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class SessionSearchRefreshTests(unittest.TestCase):
    def write_session(self, provider, path, text, session_id='session'):
        if provider == 'codex':
            records = [
                {'type': 'session_meta', 'payload': {'id': session_id}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': text}]}},
            ]
        else:
            records = [{'type': 'user', 'sessionId': session_id, 'uuid': session_id + '-message',
                        'message': {'content': text}}]
        path.write_text('\n'.join(json.dumps(record) for record in records) + '\n')

    def run_search(self, module, arguments):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            result = module.main(arguments)
        return result, output.getvalue()

    def test_default_refresh_new_and_changed_sessions_and_readonly_snapshot(self):
        for provider in ('codex', 'claude_code'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / 'sessions'
                source.mkdir()
                path = source / 'session.jsonl'
                db = root / 'index.sqlite'
                module = importlib.import_module(f'conversation_archive.{provider}.search')
                with patch.object(module, 'default_input_path', return_value=str(source)), patch.object(module, 'default_db_path', return_value=str(db)):
                    self.write_session(provider, path, 'firstword')
                    self.assertEqual(self.run_search(module, ['firstword'])[0], 0)
                    self.write_session(provider, path, 'replacementword')
                    self.write_session(provider, source / 'new.jsonl', 'brandnewword', 'new')
                    snapshot = db.read_bytes()
                    self.assertEqual(self.run_search(module, ['firstword', '--no-refresh'])[0], 0)
                    self.assertEqual(db.read_bytes(), snapshot)
                    self.assertEqual(self.run_search(module, ['replacementword'])[0], 0)
                    self.assertEqual(self.run_search(module, ['brandnewword'])[0], 0)
                    self.assertEqual(self.run_search(module, ['firstword', '--no-refresh'])[0], 1)
                    snapshot = db.read_bytes()
                    path.unlink()
                    (source / 'new.jsonl').unlink()
                    source.rmdir()
                    self.assertEqual(self.run_search(module, ['brandnewword', '--no-refresh'])[0], 0)
                    self.assertEqual(self.run_search(module, ['brandnewword'])[0], 2)
                    self.assertEqual(db.read_bytes(), snapshot)

    def test_custom_database_requires_source_and_no_refresh_does_not_create(self):
        for provider in ('codex', 'claude_code'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp:
                module = importlib.import_module(f'conversation_archive.{provider}.search')
                db = Path(temp) / 'missing.sqlite'
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    module.main(['query', '--db', str(db)])
                self.assertEqual(error.exception.code, 2)
                self.assertEqual(self.run_search(module, ['query', '--db', str(db), '--no-refresh'])[0], 2)
                self.assertFalse(db.exists())
                source = Path(temp) / 'session.jsonl'
                self.write_session(provider, source, 'customword')
                self.assertEqual(self.run_search(module, ['customword', '--db', str(db), '--input', str(source)])[0], 0)

    def test_codex_shared_refresh(self):
        from conversation_archive.codex import search as module
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'shared'
            source.mkdir()
            self.write_session('codex', source / 'session.jsonl', 'sharedword')
            with patch.object(module, 'default_shared_input_path', return_value=str(source)):
                self.assertEqual(self.run_search(module, ['sharedword', '--shared', '--db', str(Path(temp) / 'shared.sqlite')])[0], 0)

    def test_codex_refresh_refuses_claude_database(self):
        from conversation_archive.claude_code.session_index import index_sessions
        from conversation_archive.codex import search as module
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'claude.jsonl'
            db = root / 'claude.sqlite'
            self.write_session('claude_code', source, 'claudeword')
            index_sessions(source, db)
            before = db.read_bytes()
            codex = root / 'codex.jsonl'
            self.write_session('codex', codex, 'codexword')
            self.assertEqual(self.run_search(module, ['codexword', '--db', str(db), '--input', str(codex)])[0], 2)
            self.assertEqual(db.read_bytes(), before)
