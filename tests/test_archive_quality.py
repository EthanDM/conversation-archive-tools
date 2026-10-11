"""Regression evidence for session schema migration and machine approval filtering."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout

from conversation_archive.codex.session_index import SCHEMA_SQL, index_sessions, parse_session_file, repair_session_fts
from conversation_archive.codex.search import search
from conversation_archive.claude_code.session_index import index_sessions as index_claude_code


def codex_events(source: str = 'user', text: str = 'realquestion') -> list:
    return [{'type':'session_meta','payload':{'id':'synthetic','thread_source':source}},
            {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}},
            {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'realanswer'}]}}]


def legacy_schema() -> str:
    """Reproduce the original invalid external-content table and deletion triggers."""
    return SCHEMA_SQL.replace("  tokenize='unicode61 remove_diacritics 2'", "  content='messages',\n  content_rowid='id',\n  tokenize='unicode61 remove_diacritics 2'").replace('DELETE FROM messages_fts WHERE rowid=old.id;', "INSERT INTO messages_fts(messages_fts,rowid,text) VALUES ('delete',old.id,old.text);")


class QualityTests(unittest.TestCase):
    def test_guardian_metadata_and_old_envelopes_excluded_but_normal_review_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'session.jsonl'
            for source,text in [('guardian_review','realquestion'),('user','>>> RETAINED USER INSTRUCTIONS START machine'),('user','>>> TRANSCRIPT START machine'),('user','The following is the Codex agent history added since your last approval assessment. machine')]:
                path.write_text('\n'.join(json.dumps(e) for e in codex_events(source,text)))
                self.assertIsNone(parse_session_file(path))
            events=codex_events(text='Please review this code; the phrase >>> TRANSCRIPT START is a fixture')
            events[0]['payload']['source']={'subagent':{'other':'guardian'}}
            path.write_text('\n'.join(json.dumps(e) for e in events))
            self.assertIsNone(parse_session_file(path))
            events[0]['payload']['source']={'subagent':{'other':'code_review'}}
            path.write_text('\n'.join(json.dumps(e) for e in events))
            self.assertEqual(len(parse_session_file(path).messages),2)

    def test_version_change_reprocesses_unchanged_files_and_removes_guardians(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/'session.jsonl'; db=root/'index.sqlite'
            path.write_text('\n'.join(json.dumps(e) for e in codex_events()))
            index_sessions(path,db)
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE index_meta SET value='1' WHERE key='indexer_version'")
                connection.execute("UPDATE messages SET text='oldpollution' WHERE role='user'")
            with self.assertRaises(ValueError):
                index_sessions(path,db,limit=1)
            self.assertEqual(index_sessions(path,db)[:2],(1,0))
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'oldpollution'").fetchone()[0],0)
                self.assertIsNotNone(connection.execute("SELECT value FROM index_meta WHERE key='last_refresh_completed_at'").fetchone())
            path.write_text('\n'.join(json.dumps(e) for e in codex_events('guardian_review')))
            stat=path.stat()
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE sessions SET file_size=?,file_mtime_ns=?",(stat.st_size,stat.st_mtime_ns))
                connection.execute("UPDATE index_meta SET value='1' WHERE key='indexer_version'")
            index_sessions(path,db)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM sessions').fetchone()[0],0)

    def test_legacy_fts_migrates_preserving_ids_ranked_search_and_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'index.sqlite'
            connection=sqlite3.connect(db)
            connection.execute('PRAGMA foreign_keys=ON')
            connection.executescript(legacy_schema())
            connection.execute("INSERT INTO sessions VALUES ('synthetic','source','','Titleword',NULL,NULL,0,0,1,0)")
            connection.execute("INSERT INTO messages(id,session_id,seq,role,text) VALUES (42,'synthetic',1,'user','oldneedle')")
            connection.commit()
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('SELECT title FROM messages_fts').fetchall()
            repair_session_fts(connection)
            self.assertEqual(connection.execute('SELECT rowid,title FROM messages_fts').fetchone(),(42,'Titleword'))
            connection.execute("UPDATE messages SET text='newneedle' WHERE id=42")
            connection.commit()
            self.assertEqual(connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'oldneedle'").fetchone()[0],0)
            connection.close()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(search(db,'Titleword',limit=10,context=0,width=80,lines=2),0)
            with sqlite3.connect(db) as connection:
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute("DELETE FROM sessions WHERE session_id='synthetic'")
                self.assertEqual(connection.execute('SELECT count(*) FROM messages_fts').fetchone()[0],0)

    def test_fts_migration_failure_rolls_back_original_schema_and_rows(self):
        connection=sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.executescript(legacy_schema())
        connection.execute("INSERT INTO sessions VALUES ('synthetic','source','','Title',NULL,NULL,0,0,1,0)")
        connection.execute("INSERT INTO messages(id,session_id,seq,role,text) VALUES (42,'synthetic',1,'user','needle')")
        connection.commit()
        before=connection.execute("SELECT sql FROM sqlite_master WHERE name='messages_fts'").fetchone()[0]
        connection.set_authorizer(lambda action,*args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TRIGGER else sqlite3.SQLITE_OK)
        with self.assertRaises(sqlite3.DatabaseError):
            repair_session_fts(connection)
        connection.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
        self.assertEqual(connection.execute("SELECT sql FROM sqlite_master WHERE name='messages_fts'").fetchone()[0],before)
        self.assertEqual(connection.execute('SELECT id,text FROM messages').fetchone(),(42,'needle'))
        self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0],3)

    def test_claude_code_refresh_repairs_shared_legacy_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'session.jsonl'; db=root/'index.sqlite'
            source.write_text(json.dumps({'type':'user','sessionId':'synthetic','uuid':'one','message':{'content':'oldneedle'}}))
            index_claude_code(source,db)
            connection=sqlite3.connect(db)
            connection.executescript('DROP TRIGGER messages_ai; DROP TRIGGER messages_ad; DROP TRIGGER messages_au; DROP TABLE messages_fts;')
            connection.executescript(legacy_schema())
            connection.execute("INSERT INTO messages_fts(rowid,text,session_id,title,role) SELECT m.id,m.text,m.session_id,s.title,m.role FROM messages m JOIN sessions s USING(session_id)")
            connection.commit(); connection.close()
            self.assertEqual(index_claude_code(source,db)[:2],(0,1))
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT title FROM messages_fts WHERE messages_fts MATCH 'oldneedle'").fetchone()[0],'oldneedle')
