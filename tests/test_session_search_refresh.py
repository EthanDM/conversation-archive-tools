"""Exercise search refresh against synthetic sessions for both providers."""
import contextlib
import importlib
import io
import json
import os
import sqlite3
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

    def test_default_codex_search_preserves_archived_and_existing_shared_sources(self):
        from conversation_archive.codex import search as module
        from conversation_archive.codex.session_index import index_sessions
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local, archived, shared = (root / name for name in ('local', 'archived', 'shared'))
            for directory in (local, archived, shared):
                directory.mkdir()
            for directory, word in ((local, 'localword'), (archived, 'archivedword'), (shared, 'sharedword')):
                self.write_session('codex', directory / 'session.jsonl', word, word)
            db = root / 'index.sqlite'
            index_sessions(local, db, additional_inputs=(shared,))
            env = {'CODEX_SESSION_ARCHIVED_INPUT': str(archived),
                   'CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT': str(shared)}
            with patch.dict(os.environ, env), patch.object(module, 'default_input_path', return_value=str(local)), \
                 patch.object(module, 'default_db_path', return_value=str(db)):
                for word in ('localword', 'archivedword', 'sharedword'):
                    self.assertEqual(self.run_search(module, [word])[0], 0)
                with sqlite3.connect(db) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM sessions').fetchone()[0], 3)
                self.assertEqual(self.run_search(module, ['sharedword', '--shared'])[0], 2)
                self.assertEqual(self.run_search(module, ['archivedword', '--no-refresh'])[0], 0)
                (archived / 'session.jsonl').unlink()
                archived.rmdir()
                self.assertEqual(self.run_search(module, ['sharedword'])[0], 2)
                self.assertEqual(self.run_search(module, ['archivedword', '--no-refresh'])[0], 0)
                self.assertEqual(self.run_search(module, ['sharedword', '--no-refresh'])[0], 0)

    def test_codex_cli_indexes_all_roots_and_default_index_keeps_shared(self):
        from conversation_archive.codex import index as module
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local, archived, shared = (root / name for name in ('local', 'archived', 'shared'))
            for directory in (local, archived, shared):
                directory.mkdir()
                self.write_session('codex', directory / 'session.jsonl', directory.name + 'word', directory.name)
            db = root / 'index.sqlite'
            env = {'CODEX_SESSION_INPUT': str(local), 'CODEX_SESSION_ARCHIVED_INPUT': str(archived),
                   'CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT': str(shared), 'CODEX_SESSION_DB': str(db)}
            with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(['--shared', '--include-local']), 0)
                self.assertEqual(module.main([]), 0)
            with sqlite3.connect(db) as connection:
                self.assertEqual({row[0] for row in connection.execute('SELECT session_id FROM sessions')},
                                 {'local', 'archived', 'shared'})

    def test_cli_refuses_scope_expanded_after_source_selection(self):
        from conversation_archive.codex import index as module
        from conversation_archive.codex.session_index import index_sessions
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local, shared = root / 'local', root / 'shared'
            for directory in (local, shared):
                directory.mkdir()
                self.write_session('codex', directory / 'session.jsonl', directory.name, directory.name)
            db = root / 'index.sqlite'
            index_sessions(local, db)
            env = {'CODEX_SESSION_INPUT': str(local), 'CODEX_SESSION_DB': str(db),
                   'CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT': str(shared)}

            def expand_then_index(*args, **kwargs):
                index_sessions(local, db, additional_inputs=(shared,))
                return index_sessions(*args, **kwargs)

            with patch.dict(os.environ, env), patch.object(module, 'index_sessions', side_effect=expand_then_index):
                with self.assertRaisesRegex(ValueError, 'other source roots'):
                    module.main([])
            with sqlite3.connect(db) as connection:
                self.assertEqual({row[0] for row in connection.execute('SELECT session_id FROM sessions')},
                                 {'local', 'shared'})

    def test_search_refuses_scope_expanded_after_source_selection(self):
        from conversation_archive.codex import search as module
        from conversation_archive.codex.session_index import index_sessions
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local, shared = root / 'local', root / 'shared'
            for directory in (local, shared):
                directory.mkdir()
                self.write_session('codex', directory / 'session.jsonl', directory.name, directory.name)
            db = root / 'index.sqlite'
            index_sessions(local, db)
            env = {'CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT': str(shared)}

            def expand_then_index(*args, **kwargs):
                index_sessions(local, db, additional_inputs=(shared,))
                return index_sessions(*args, **kwargs)

            with patch.dict(os.environ, env), patch.object(module, 'default_input_path', return_value=str(local)), \
                 patch.object(module, 'default_db_path', return_value=str(db)), \
                 patch.object(module, 'index_sessions', side_effect=expand_then_index):
                self.assertEqual(self.run_search(module, ['local'])[0], 2)
                self.assertEqual(self.run_search(module, ['shared', '--no-refresh'])[0], 0)
            with sqlite3.connect(db) as connection:
                self.assertEqual({row[0] for row in connection.execute('SELECT session_id FROM sessions')},
                                 {'local', 'shared'})

    def test_scope_validation_holds_write_lock_until_pruning(self):
        from conversation_archive.codex import session_index
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'local'
            source.mkdir()
            self.write_session('codex', source / 'session.jsonl', 'localword')
            db = root / 'index.sqlite'
            session_index.index_sessions(source, db)
            guard = session_index.ensure_scope_not_narrowed
            attempts = []

            def check_and_attempt_write(connection, roots):
                guard(connection, roots)
                with sqlite3.connect(db, timeout=0) as concurrent:
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                        concurrent.execute("INSERT INTO index_meta VALUES ('other', 'value')")
                attempts.append(True)

            with patch.object(session_index, 'ensure_scope_not_narrowed', side_effect=check_and_attempt_write):
                session_index.index_sessions(source, db, preserve_scope=True)
            self.assertEqual(attempts, [True])

    def test_default_reset_refuses_invalid_scope_without_losing_shared_history(self):
        from conversation_archive.codex import index as module
        from conversation_archive.codex.session_index import index_sessions
        for invalid in ('not json', '{"not": "a list"}', '[]'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                local, shared = root / 'local', root / 'shared'
                for directory in (local, shared):
                    directory.mkdir()
                    self.write_session('codex', directory / 'session.jsonl', directory.name, directory.name)
                db = root / 'index.sqlite'
                index_sessions(local, db, additional_inputs=(shared,))
                with sqlite3.connect(db) as connection:
                    connection.execute("UPDATE index_meta SET value=? WHERE key='source_roots'", (invalid,))
                env = {'CODEX_SESSION_INPUT': str(local), 'CODEX_SESSION_DB': str(db),
                       'CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT': str(shared)}
                with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ValueError, 'explicit reset scope'):
                        module.main(['--reset'])
                    with sqlite3.connect(db) as connection:
                        self.assertEqual({row[0] for row in connection.execute('SELECT session_id FROM sessions')},
                                         {'local', 'shared'})
                    self.assertEqual(module.main(['--shared', '--include-local', '--reset']), 0)
                with sqlite3.connect(db) as connection:
                    self.assertEqual({row[0] for row in connection.execute('SELECT session_id FROM sessions')},
                                     {'local', 'shared'})

    def test_cli_reset_recovers_invalid_source_root_metadata(self):
        from conversation_archive.codex import index as module
        from conversation_archive.codex.session_index import index_sessions
        for invalid in ('not json', '{"not": "a list"}', '[]'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                local, archived, shared = (root / name for name in ('local', 'archived', 'shared'))
                for directory in (local, archived, shared):
                    directory.mkdir()
                    self.write_session('codex', directory / 'session.jsonl', directory.name, directory.name)
                db = root / 'index.sqlite'
                index_sessions(local, db)
                with sqlite3.connect(db) as connection:
                    connection.execute("UPDATE index_meta SET value=? WHERE key='source_roots'", (invalid,))
                env = {'CODEX_SESSION_INPUT': str(local), 'CODEX_SESSION_ARCHIVED_INPUT': str(archived),
                       'CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT': str(shared), 'CODEX_SESSION_DB': str(db)}
                with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(['--shared', '--include-local', '--reset']), 0)
                with sqlite3.connect(db) as connection:
                    self.assertEqual({row[0] for row in connection.execute('SELECT session_id FROM sessions')},
                                     {'local', 'archived', 'shared'})

    def test_cli_reset_recovers_corrupt_index_with_all_configured_roots(self):
        from conversation_archive.codex import index as module
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local, archived, shared = (root / name for name in ('local', 'archived', 'shared'))
            for directory in (local, archived, shared):
                directory.mkdir()
                self.write_session('codex', directory / 'session.jsonl', directory.name + 'word', directory.name)
            db = root / 'index.sqlite'
            db.write_bytes(b'not sqlite')
            env = {'CODEX_SESSION_INPUT': str(local), 'CODEX_SESSION_ARCHIVED_INPUT': str(archived),
                   'CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT': str(shared), 'CODEX_SESSION_DB': str(db)}
            with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(['--shared', '--include-local', '--reset']), 0)
            with sqlite3.connect(db) as connection:
                self.assertEqual({row[0] for row in connection.execute('SELECT session_id FROM sessions')},
                                 {'local', 'archived', 'shared'})

    def test_missing_root_list_preserves_legacy_single_root_fallback(self):
        from conversation_archive.codex import source_roots
        from conversation_archive.codex.session_index import index_sessions
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'sessions'
            source.mkdir()
            self.write_session('codex', source / 'session.jsonl', 'legacy')
            db = root / 'index.sqlite'
            index_sessions(source, db)
            with sqlite3.connect(db) as connection:
                connection.execute("DELETE FROM index_meta WHERE key='source_roots'")
            self.assertEqual(source_roots.indexed_roots(db), {source.resolve()})

    def test_default_archive_is_optional_then_required_after_first_index(self):
        from conversation_archive.codex import source_roots
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local = root / '.codex' / 'sessions'
            local.mkdir(parents=True)
            archived = local.parent / 'archived_sessions'
            db = root / 'index.sqlite'
            with patch.object(source_roots, 'DEFAULT_INPUT_PATH', local), \
                 patch.dict(os.environ, {}, clear=True):
                self.assertEqual(source_roots.local_additional_roots(local, db), ())
                archived.mkdir()
                self.assertEqual(source_roots.local_additional_roots(local, db), (archived.resolve(),))
                from conversation_archive.codex.session_index import index_sessions
                index_sessions(local, db, additional_inputs=(archived,))
                archived.rmdir()
                self.assertEqual(source_roots.local_additional_roots(local, db), (archived.resolve(),))
                with self.assertRaises(FileNotFoundError):
                    index_sessions(local, db, additional_inputs=source_roots.local_additional_roots(local, db))

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
