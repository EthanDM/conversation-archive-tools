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

    @unittest.skipUnless(importlib.util.find_spec('mcp'),'optional MCP SDK not installed')
    def test_real_stdio_refresh_then_search(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        shared=self.root/'shared-stdio'
        remote=write_source(shared,'codex','remoteprotocolneedle')
        remote.write_text(remote.read_text().replace('synthetic','remote-protocol-session'))
        async def exercise():
            env=dict(os.environ,**self.env)
            env['CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT']=str(shared)
            env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
            params=StdioServerParameters(command=sys.executable,args=['-m','conversation_archive.mcp'],env=env,cwd=tempfile.gettempdir())
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as client:
                    await client.initialize()
                    for provider in self.sources:
                        args={'provider':provider,'include_shared':provider=='codex'}
                        result=await client.call_tool('refresh_local_sessions',args)
                        self.assertFalse(result.isError)
                        self.assertEqual(result.structuredContent['indexed_files'],2 if provider=='codex' else 1)
                        if provider=='codex':
                            self.assertEqual(result.structuredContent['retained_messages'],2)
                        found=await client.call_tool('search_conversations',{'provider':provider,'query':'originalneedle'})
                        self.assertEqual(len(found.structuredContent['items']),1)
                        retry=await client.call_tool('refresh_local_sessions',args)
                        self.assertEqual(retry.structuredContent['unchanged_files'],2 if provider=='codex' else 1)
        asyncio.run(asyncio.wait_for(exercise(),30))
