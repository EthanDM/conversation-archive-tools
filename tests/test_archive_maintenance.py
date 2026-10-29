"""Refresh configured synthetic sources, including real stdio calls; no live history."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import zstandard

from conversation_archive import maintenance, retrieval


def write_source(root: Path, provider: str, text: str) -> Path:
    """Create one provider-native synthetic session for exercising existing indexers."""
    root.mkdir(exist_ok=True)
    path=root/'session.jsonl'
    if provider == 'codex':
        events=[{'type':'session_meta','payload':{'id':'synthetic','cwd':'/synthetic/project'}},
                {'type':'response_item','timestamp':'2026-10-11T12:00:00Z','payload':{
                    'type':'message','role':'user','content':[{'type':'input_text','text':text}]}}]
    else:
        events=[{'type':'user','sessionId':'synthetic','uuid':text,'cwd':'/synthetic/project',
                 'timestamp':'2026-10-11T12:00:00Z','message':{'content':text}}]
    path.write_text('\n'.join(json.dumps(event) for event in events)+'\n')
    return path


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.env={}
        self.sources={}
        self.databases={}
        for provider,prefix in [('codex','CODEX_SESSION'),('claude_code','CLAUDE_CODE_SESSION')]:
            self.sources[provider]=write_source(self.root/provider,provider,'originalneedle')
            self.databases[provider]=self.root/(provider+'.sqlite')
            self.env[prefix+'_INPUT']=str(self.sources[provider].parent)
            self.env[prefix+'_DB']=str(self.databases[provider])

    def test_incremental_refresh_changes_and_missing_source_policy(self):
        with patch.dict(os.environ,self.env):
            for provider,path in self.sources.items():
                original=path.read_bytes()
                result=maintenance.refresh_local_sessions(provider)
                self.assertEqual(result['indexed_files'],1)
                self.assertEqual(result['retained_messages'],1)
                self.assertEqual(path.read_bytes(),original)
                self.assertEqual(maintenance.refresh_local_sessions(provider)['unchanged_files'],1)
                write_source(path.parent,provider,'changedneedle longer')
                self.assertEqual(maintenance.refresh_local_sessions(provider)['indexed_files'],1)
                self.assertEqual(len(retrieval.search_conversations(provider,'changedneedle')['items']),1)
                self.assertEqual(retrieval.search_conversations(provider,'originalneedle')['items'],[])
                path.unlink()
                result=maintenance.refresh_local_sessions(provider)
                self.assertEqual(result['retained_messages'],0 if provider=='codex' else 1)

    def test_wrong_provider_other_source_missing_and_busy_refused(self):
        with patch.dict(os.environ,self.env):
            maintenance.refresh_local_sessions('claude_code')
            with patch.dict(os.environ,{'CODEX_SESSION_DB':str(self.databases['claude_code'])}):
                with self.assertRaises(ValueError):
                    maintenance.refresh_local_sessions('codex')
            maintenance.refresh_local_sessions('codex')
            other=self.root/'other'
            other.mkdir()
            before=self.databases['codex'].read_bytes()
            with patch.dict(os.environ,{'CODEX_SESSION_INPUT':str(other)}):
                with self.assertRaises(ValueError):
                    maintenance.refresh_local_sessions('codex')
            self.assertEqual(before,self.databases['codex'].read_bytes())
            with patch.dict(os.environ,{'CODEX_SESSION_INPUT':str(self.root/'missing')}):
                with self.assertRaises(ValueError):
                    maintenance.refresh_local_sessions('codex')
            maintenance._REFRESH_LOCK.acquire()
            try:
                with self.assertRaises(ValueError):
                    maintenance.refresh_local_sessions('codex')
            finally:
                maintenance._REFRESH_LOCK.release()
            with self.assertRaises(ValueError):
                maintenance.refresh_local_sessions('chatgpt')

    def test_shared_plus_local_union_preserves_remote_history_and_deduplicates_ids(self):
        shared=self.root/'shared'
        shared_path=write_source(shared,'codex','remoteneedle')
        shared_path.write_text(shared_path.read_text().replace('synthetic','remote-session'))
        env=dict(self.env,CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT=str(shared))
        with patch.dict(os.environ,env):
            result=maintenance.refresh_local_sessions('codex',include_shared=True)
            self.assertTrue(result['includes_shared'])
            self.assertEqual(result['retained_messages'],2)
            self.assertEqual(len(retrieval.search_conversations('codex','remoteneedle')['items']),1)
            self.assertEqual(len(retrieval.search_conversations('codex','originalneedle')['items']),1)
            with self.assertRaises(ValueError):
                maintenance.refresh_local_sessions('codex')
            before=self.databases['codex'].read_bytes()
            with patch.dict(os.environ,{'CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT':str(self.root/'missing-shared')}):
                with self.assertRaises(ValueError):
                    maintenance.refresh_local_sessions('codex',include_shared=True)
            self.assertEqual(before,self.databases['codex'].read_bytes())
            with self.assertRaises(ValueError):
                maintenance.refresh_local_sessions('claude_code',include_shared=True)
            duplicate=shared/'duplicate.jsonl'
            duplicate.write_bytes(self.sources['codex'].read_bytes())
            result=maintenance.refresh_local_sessions('codex',include_shared=True)
            self.assertEqual(result['retained_messages'],2)
            self.assertEqual(len(retrieval.search_conversations('codex','originalneedle')['items']),1)

    def test_codex_refresh_refuses_scope_expanded_after_precheck(self):
        from conversation_archive.codex.session_index import index_sessions
        shared = self.root / 'shared-race'
        shared_path = write_source(shared, 'codex', 'sharedrace')
        shared_path.write_text(shared_path.read_text().replace('synthetic', 'shared-race'))
        local = self.sources['codex'].parent
        db = self.databases['codex']
        with patch.dict(os.environ, self.env):
            maintenance.refresh_local_sessions('codex')

            def expand_then_index(*args, **kwargs):
                index_sessions(local, db, additional_inputs=(shared,))
                return index_sessions(*args, **kwargs)

            with patch.object(maintenance, 'index_codex', side_effect=expand_then_index):
                with self.assertRaisesRegex(ValueError, 'other source roots'):
                    maintenance.refresh_local_sessions('codex')
            self.assertEqual(len(retrieval.search_conversations('codex', 'originalneedle')['items']), 1)
            self.assertEqual(len(retrieval.search_conversations('codex', 'sharedrace')['items']), 1)

    def test_move_during_root_enumeration_preserves_and_reconciles_history(self):
        from conversation_archive.codex import session_index
        archived = self.root / 'archived-move'
        moved = write_source(archived, 'codex', 'archivedmove')
        moved.write_text(moved.read_text().replace('synthetic', 'archived-move'))
        shared = self.root / 'shared-move'
        shared_path = write_source(shared, 'codex', 'sharedmove')
        shared_path.write_text(shared_path.read_text().replace('synthetic', 'shared-move'))
        local = self.sources['codex'].parent
        db = self.databases['codex']
        env = dict(self.env, CODEX_SESSION_ARCHIVED_INPUT=str(archived),
                   CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT=str(shared))
        with patch.dict(os.environ, env):
            maintenance.refresh_local_sessions('codex', include_shared=True)
            destination = local / 'moved.jsonl'
            original_iter = session_index.iter_session_files
            def move_before_archived_scan(path):
                if path == archived.resolve():
                    moved.rename(destination)
                yield from original_iter(path)
            with patch.object(session_index, 'iter_session_files', side_effect=move_before_archived_scan):
                result = maintenance.refresh_local_sessions('codex', include_shared=True)
            self.assertEqual(result['missing_source_policy'], 'retain_local_missing_rows')
            for word in ('originalneedle', 'archivedmove', 'sharedmove'):
                self.assertEqual(len(retrieval.search_conversations('codex', word)['items']), 1)
            maintenance.refresh_local_sessions('codex', include_shared=True)
            with retrieval.open_index('codex', db) as connection:
                path = connection.execute("SELECT source_path FROM sessions WHERE session_id='archived-move'").fetchone()[0]
                self.assertEqual(path, str(destination.resolve()))
            destination.unlink()
            shared_path.unlink()
            maintenance.refresh_local_sessions('codex', include_shared=True)
            self.assertEqual(len(retrieval.search_conversations('codex', 'archivedmove')['items']), 1)
            self.assertEqual(retrieval.search_conversations('codex', 'sharedmove')['items'], [])
            session_index.index_sessions(local, db, additional_inputs=(archived, shared), reset=True)
            self.assertEqual(retrieval.search_conversations('codex', 'archivedmove')['items'], [])

    def test_equal_timestamp_moved_session_can_extend_indexed_history(self):
        archived = self.root / 'archived-extended'
        archived.mkdir()
        local = self.sources['codex']
        env = dict(self.env, CODEX_SESSION_ARCHIVED_INPUT=str(archived))
        with patch.dict(os.environ, env):
            maintenance.refresh_local_sessions('codex')
            old_time = local.stat().st_mtime_ns
            moved = archived / 'moved.jsonl'
            local.rename(moved)
            event = {'type': 'response_item', 'timestamp': '2026-10-12T12:00:00Z',
                     'payload': {'type': 'message', 'role': 'user',
                                 'content': [{'type': 'input_text', 'text': 'appendedneedle'}]}}
            with moved.open('a') as stream:
                stream.write(json.dumps(event) + '\n')
            os.utime(moved, ns=(old_time, old_time))
            maintenance.refresh_local_sessions('codex')
            for needle in ('originalneedle', 'appendedneedle'):
                self.assertGreaterEqual(len(retrieval.search_conversations('codex', needle)['items']), 1)
            with retrieval.open_index('codex', self.databases['codex']) as connection:
                self.assertEqual(connection.execute('SELECT source_path FROM sessions').fetchone()[0], str(moved.resolve()))
                self.assertEqual(connection.execute('SELECT count(*) FROM messages').fetchone()[0], 2)

    def test_equal_timestamp_shorter_or_conflicting_snapshot_is_not_indexed(self):
        for variant in ('shorter', 'conflicting'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                local = root / 'local'
                archived = root / 'archived'
                original = write_source(local, 'codex', 'firstneedle')
                archived.mkdir()
                extra = {'type': 'response_item', 'timestamp': '2026-10-12T12:00:00Z',
                         'payload': {'type': 'message', 'role': 'user',
                                     'content': [{'type': 'input_text', 'text': 'secondneedle'}]}}
                first = original.read_text()
                original.write_text(first + json.dumps(extra) + '\n')
                stale = archived / 'stale.jsonl'
                if variant == 'conflicting':
                    extra['payload']['content'][0]['text'] = 'conflictneedle'
                    first += json.dumps(extra) + '\n'
                stale.write_text(first)
                os.utime(stale, ns=(1_000_000_000, 1_000_000_000))
                os.utime(original, ns=(2_000_000_000, 2_000_000_000))
                db = root / 'index.sqlite'
                env = {'CODEX_SESSION_INPUT': str(local), 'CODEX_SESSION_DB': str(db),
                       'CODEX_SESSION_ARCHIVED_INPUT': str(archived)}
                with patch.dict(os.environ, env):
                    maintenance.refresh_local_sessions('codex')
                    original.unlink()
                    os.utime(stale, ns=(2_000_000_000, 2_000_000_000))
                    maintenance.refresh_local_sessions('codex')
                    self.assertEqual(len(retrieval.search_conversations('codex', 'secondneedle')['items']), 1)
                    self.assertEqual(retrieval.search_conversations('codex', 'conflictneedle')['items'], [])

    def test_equal_timestamp_stale_local_duplicate_does_not_replace_retained_history(self):
        archived = self.root / 'archived-equal-stale'
        archived_path = write_source(archived, 'codex', 'staleduplicate')
        local = self.sources['codex']
        os.utime(archived_path, ns=(1_000_000_000, 1_000_000_000))
        os.utime(local, ns=(2_000_000_000, 2_000_000_000))
        env = dict(self.env, CODEX_SESSION_ARCHIVED_INPUT=str(archived))
        with patch.dict(os.environ, env):
            maintenance.refresh_local_sessions('codex')
            local.unlink()
            os.utime(archived_path, ns=(2_000_000_000, 2_000_000_000))
            maintenance.refresh_local_sessions('codex')
            self.assertEqual(len(retrieval.search_conversations('codex', 'originalneedle')['items']), 1)
            self.assertEqual(retrieval.search_conversations('codex', 'staleduplicate')['items'], [])
            with retrieval.open_index('codex', self.databases['codex']) as connection:
                self.assertEqual(connection.execute('SELECT source_path FROM sessions').fetchone()[0], str(local.resolve()))

    def test_missing_local_history_is_not_replaced_by_older_shared_copy(self):
        archived = self.root / 'archived-stale'
        archived.mkdir()
        shared = self.root / 'shared-stale'
        shared_path = write_source(shared, 'codex', 'oldcopy')
        local = self.sources['codex']
        os.utime(shared_path, ns=(1_000_000_000, 1_000_000_000))
        os.utime(local, ns=(2_000_000_000, 2_000_000_000))
        env = dict(self.env, CODEX_SESSION_ARCHIVED_INPUT=str(archived),
                   CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT=str(shared))
        with patch.dict(os.environ, env):
            maintenance.refresh_local_sessions('codex', include_shared=True)
            local.unlink()
            maintenance.refresh_local_sessions('codex', include_shared=True)
            self.assertEqual(len(retrieval.search_conversations('codex', 'originalneedle')['items']), 1)
            self.assertEqual(retrieval.search_conversations('codex', 'oldcopy')['items'], [])

    def test_archived_local_and_shared_sources_survive_refresh_and_move(self):
        archived = self.root / 'archived'
        archived_path = write_source(archived, 'codex', 'archivedneedle')
        archived_path.write_text(archived_path.read_text().replace('synthetic', 'archived-session'))
        compressed = archived_path.with_name(archived_path.name + '.zst')
        compressed.write_bytes(zstandard.ZstdCompressor().compress(archived_path.read_bytes()))
        archived_path.unlink()
        shared = self.root / 'shared'
        shared_path = write_source(shared, 'codex', 'sharedneedle')
        shared_path.write_text(shared_path.read_text().replace('synthetic', 'shared-session'))
        env = dict(self.env, CODEX_SESSION_ARCHIVED_INPUT=str(archived),
                   CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT=str(shared))
        with patch.dict(os.environ, env):
            result = maintenance.refresh_local_sessions('codex', include_shared=True)
            self.assertEqual(result['retained_messages'], 3)
            for needle in ('originalneedle', 'archivedneedle', 'sharedneedle'):
                self.assertEqual(len(retrieval.search_conversations('codex', needle)['items']), 1)
            local_path = self.sources['codex']
            moved = archived / 'moved.jsonl'
            moved.write_bytes(local_path.read_bytes())
            local_path.unlink()
            result = maintenance.refresh_local_sessions('codex', include_shared=True)
            self.assertEqual(result['retained_messages'], 3)
            self.assertEqual(len(retrieval.search_conversations('codex', 'originalneedle')['items']), 1)
            moved.unlink()
            compressed.unlink()
            archived.rmdir()
            with self.assertRaises((ValueError, FileNotFoundError)):
                maintenance.refresh_local_sessions('codex', include_shared=True)
            for needle in ('originalneedle', 'archivedneedle', 'sharedneedle'):
                self.assertEqual(len(retrieval.search_conversations('codex', needle)['items']), 1)

    @unittest.skipUnless(importlib.util.find_spec('mcp'),'optional MCP SDK not installed')
    def test_real_stdio_refresh_then_search(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        archived = self.root / 'archived-stdio'
        archived_source = write_source(archived, 'codex', 'archivedprotocolneedle')
        archived_source.write_text(archived_source.read_text().replace('synthetic', 'archived-protocol-session'))
        shared=self.root/'shared-stdio'
        remote=write_source(shared,'codex','remoteprotocolneedle')
        remote.write_text(remote.read_text().replace('synthetic','remote-protocol-session'))
        compressed = remote.with_name(remote.name + '.zst')
        compressed.write_bytes(zstandard.ZstdCompressor().compress(remote.read_bytes()))
        remote.unlink()
        async def exercise():
            env=dict(os.environ,**self.env)
            env['CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT']=str(shared)
            env['CODEX_SESSION_ARCHIVED_INPUT']=str(archived)
            env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
            params=StdioServerParameters(command=sys.executable,args=['-m','conversation_archive.mcp'],env=env,cwd=tempfile.gettempdir())
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as client:
                    await client.initialize()
                    for provider in self.sources:
                        args={'provider':provider,'include_shared':provider=='codex'}
                        result=await client.call_tool('refresh_local_sessions',args)
                        self.assertFalse(result.isError)
                        self.assertEqual(result.structuredContent['indexed_files'],3 if provider=='codex' else 1)
                        if provider=='codex':
                            self.assertEqual(result.structuredContent['retained_messages'],3)
                        found=await client.call_tool('search_conversations',{'provider':provider,'query':'originalneedle'})
                        self.assertEqual(len(found.structuredContent['items']),1)
                        retry=await client.call_tool('refresh_local_sessions',args)
                        self.assertEqual(retry.structuredContent['unchanged_files'],3 if provider=='codex' else 1)
        asyncio.run(asyncio.wait_for(exercise(),30))
