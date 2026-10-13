"""Synthetic provider coverage and real stdio tests; never use personal archives."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from conversation_archive import historical, retrieval
from conversation_archive.chatgpt.historical_index import SCHEMA as HISTORICAL_SCHEMA, insert_document
from conversation_archive.chatgpt.index import SCHEMA_SQL as CHATGPT_SCHEMA
from conversation_archive.codex.session_index import SCHEMA_SQL as SESSION_SCHEMA
from conversation_archive.claude.index import SCHEMA as CLAUDE_SCHEMA


def fixture(root: Path, provider: str) -> Path:
    path = root / (provider + '.sqlite')
    connection = sqlite3.connect(path)
    if provider == 'claude':
        for statement in CLAUDE_SCHEMA:
            connection.execute(statement)
        connection.executemany('INSERT INTO index_meta VALUES (?,?)', [('provider','claude'), ('schema_version','1')])
        connection.execute("INSERT INTO conversations VALUES ('conv','Archive title','2026-01-01',NULL,'fixture')")
    elif provider == 'chatgpt':
        connection.executescript(CHATGPT_SCHEMA)
        connection.execute("INSERT INTO index_meta VALUES ('indexer_version_applied','1')")
        connection.execute("INSERT INTO conversations(conversation_id,title) VALUES ('conv','Archive title')")
    else:
        connection.executescript(SESSION_SCHEMA)
        if provider == 'claude_code':
            connection.executescript('ALTER TABLE messages ADD COLUMN uuid TEXT; ALTER TABLE messages ADD COLUMN parent_uuid TEXT; ALTER TABLE messages ADD COLUMN is_sidechain INTEGER DEFAULT 0;')
            connection.executemany('INSERT INTO index_meta VALUES (?,?)', [('provider','claude_code'),('schema_version','1')])
        else:
            connection.execute("INSERT INTO index_meta VALUES ('indexer_version','1')")
        connection.execute("INSERT INTO sessions(session_id,source_path,title,file_size,file_mtime_ns,user_message_count,assistant_message_count) VALUES ('conv','fixture','Archive title',0,0,1,1)")
    for index, role, text in [(1,'user','needle ' + 'x'*9000),(2,'assistant','needle response')]:
        if provider == 'claude':
            connection.execute('INSERT INTO messages(id,uuid,conversation_uuid,parent_uuid,seq,role,created_at,text) VALUES (?,?,?,?,?,?,?,?)', (index,str(index),'conv','1' if index==2 else None,index,role,'2026-01-01T12:00:00Z',text))
            connection.execute('INSERT INTO messages_fts(rowid,text,title) VALUES (?,?,?)',(index,text,'Archive title'))
        elif provider == 'chatgpt':
            connection.execute('INSERT INTO messages(id,conversation_id,seq,node_id,message_id,role,create_time_iso,text) VALUES (?,?,?,?,?,?,?,?)', (index,'conv',index,str(index),str(index),role,'2026-01-01T12:00:00Z',text))
        else:
            connection.execute('INSERT INTO messages(id,session_id,seq,role,create_time_iso,text) VALUES (?,?,?,?,?,?)',(index,'conv',index,role,'2026-01-01T12:00:00Z',text))
            if provider == 'claude_code':
                connection.execute('UPDATE messages SET uuid=?, parent_uuid=?, is_sidechain=? WHERE id=?',(str(index),'1' if index==2 else None,index==2,index))
    connection.commit()
    connection.close()
    return path


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.historical_path = Path(self.directory.name)/'historical.sqlite'
        with sqlite3.connect(self.historical_path) as connection:
            connection.executescript(HISTORICAL_SCHEMA)
            for title,text,source in [('Old','historicalneedle '+'x'*17000,'exports/text-history/2025/conversations.json'),('Other','historicalneedle another','exports/legacy/other.md')]:
                insert_document(connection,title=title,text=text,source_path=source,
                                source_kind='older-official-only',format_name='json',conversation_id='old-conv')
            self.assertFalse(insert_document(connection,title='Duplicate',text='historicalneedle another',source_path='duplicate',source_kind='legacy-zip',format_name='md'))
        environment=patch.dict(os.environ,{'CHATGPT_HISTORICAL_DB':str(self.historical_path)})
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = {provider:fixture(Path(self.directory.name), provider) for provider in retrieval.PROVIDERS}

    def test_all_providers_pagination_context_and_readonly(self):
        for provider,path in self.paths.items():
            with self.subTest(provider=provider):
                digest = hashlib.sha256(path.read_bytes()).digest()
                first = retrieval.search_conversations(provider,'needle',limit=1,path=path)
                self.assertTrue(first['has_more'])
                second = retrieval.search_conversations(provider,'needle',limit=1,offset=first['next_offset'],path=path)
                self.assertFalse(second['has_more'])
                self.assertNotEqual(first['items'][0]['message_index_id'],second['items'][0]['message_index_id'])
                result = retrieval.read_conversation(provider,'conv',path=path)
                self.assertEqual([item['role'] for item in result['items']],['user','assistant'])
                self.assertTrue(result['items'][0]['truncated'])
                chunks=[]
                offset=0
                while offset is not None:
                    result=retrieval.read_message_context(provider,1,text_offset=offset,path=path)
                    hit=next(item for item in result['items'] if item['message_index_id']==1)
                    chunks.append(hit['text'])
                    offset=hit['next_text_offset']
                self.assertEqual(''.join(chunks),'needle '+'x'*9000)
                self.assertEqual(retrieval.search_conversations(provider,'absent',path=path)['items'],[])
                self.assertEqual(len(retrieval.search_conversations(provider,'needle',role='user',start_date='2026-01-01',end_date='2026-01-01',path=path)['items']),1)
                self.assertEqual(digest,hashlib.sha256(path.read_bytes()).digest())
                with retrieval.open_index(provider,path) as connection:
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute("DELETE FROM messages")
        status=retrieval.archive_status(self.paths)
        self.assertTrue(all(item['available'] for item in status['providers']))
        self.assertTrue(all(item['last_refresh'] is None for item in status['providers']))

    def test_relevance_and_match_labels_across_providers(self):
        for provider, path in self.paths.items():
            with self.subTest(provider=provider):
                ranked = retrieval.search_conversations(provider, 'needle', path=path)
                self.assertEqual(ranked['items'][0]['message_index_id'], 2)
                self.assertTrue(all(hit['match_type'] == 'message' for hit in ranked['items']))
                with sqlite3.connect(path) as connection:
                    table = 'sessions' if provider in {'codex', 'claude_code'} else 'conversations'
                    connection.execute(f"UPDATE {table} SET title='needle topic'")
                    connection.execute("UPDATE messages SET text='unrelated reply' WHERE id=2")
                    if provider == 'claude':
                        connection.execute('DELETE FROM messages_fts WHERE rowid=2')
                        connection.execute("INSERT INTO messages_fts(rowid,text,title) VALUES (2,'unrelated reply','needle topic')")
                first = retrieval.search_conversations(provider, 'needle', limit=1, path=path)
                self.assertEqual(first['items'][0]['message_index_id'], 1)
                self.assertEqual(first['items'][0]['match_type'], 'message_and_title')
                second = retrieval.search_conversations(provider, 'needle', limit=1,
                                                       offset=first['next_offset'], path=path)
                self.assertEqual(second['items'][0]['match_type'], 'title')
                self.assertIsNone(second['items'][0]['relevance_score'])
                grouped = retrieval.search_topic(provider, 'needle', role=None, path=path)
                self.assertEqual(grouped['matching_messages'], 2)
                self.assertEqual({hit['match_type'] for hit in grouped['items'][0]['samples']},
                                 {'message_and_title', 'title'})

    def test_provider_rejection_missing_and_invalid(self):
        for provider in self.paths:
            for other,path in self.paths.items():
                if provider != other:
                    with self.assertRaises((ValueError,sqlite3.Error)):
                        retrieval.search_conversations(provider,'needle',path=path)
        missing=Path(self.directory.name)/'missing.sqlite'
        with self.assertRaises(sqlite3.Error):
            retrieval.search_conversations('codex','needle',path=missing)
        self.assertFalse(missing.exists())
        for args in ({'limit':51},{'offset':-1},{'role':'system'},{'start_date':'2026-02-30'},{'start_date':'2026-02-01','end_date':'2026-01-01'}):
            with self.assertRaises(ValueError):
                retrieval.search_conversations('codex','needle',path=self.paths['codex'],**args)
        with self.assertRaises(ValueError):
            retrieval.read_conversation('codex','missing',path=self.paths['codex'])

    def test_browse_and_cross_provider_partial_pages(self):
        for provider,path in self.paths.items():
            result=retrieval.list_conversations(provider,title='Archive',path=path)
            self.assertEqual(result['items'][0]['message_count'],2)
            self.assertNotIn('text',result['items'][0])
            self.assertEqual(retrieval.list_conversations(provider,title='%',path=path)['items'],[])
            self.assertEqual(retrieval.list_conversations(provider,start_date='2026-01-02',path=path)['items'],[])
        with sqlite3.connect(self.paths['codex']) as connection:
            connection.execute("UPDATE sessions SET cwd='/synthetic/project'")
            connection.execute("INSERT INTO sessions(session_id,source_path,title,file_size,file_mtime_ns,user_message_count,assistant_message_count) VALUES ('empty','empty','Empty',0,0,0,0)")
        result=retrieval.list_conversations('codex',limit=1,path=self.paths['codex'])
        self.assertTrue(result['has_more'])
        self.assertEqual(retrieval.list_conversations('codex',limit=1,offset=result['next_offset'],path=self.paths['codex'])['items'][0]['message_count'],0)
        self.assertEqual(len(retrieval.list_conversations('codex',cwd='project',path=self.paths['codex'])['items']),1)
        with self.assertRaises(ValueError):
            retrieval.list_conversations('claude',cwd='project',path=self.paths['claude'])
        result=retrieval.search_all_conversations('needle',limit=1,paths=self.paths)
        self.assertFalse(result['partial'])
        self.assertTrue(all(page['has_more'] for page in result['providers']))
        paths=dict(self.paths,claude=Path(self.directory.name)/'missing.sqlite')
        result=retrieval.search_all_conversations('needle',paths=paths)
        self.assertTrue(result['partial'])
        self.assertEqual(sum(page['available'] for page in result['providers']),3)
        self.assertFalse(paths['claude'].exists())
        with self.assertRaises(ValueError):
            retrieval.search_all_conversations('needle',limit=51,paths=self.paths)

    def test_grouped_topic_counts_samples_filters_and_pagination(self):
        for provider,path in self.paths.items():
            result=retrieval.search_topic(provider,'needle',path=path)
            self.assertEqual(result['matching_messages'],1)
            self.assertEqual(result['matching_conversations'],1)
            self.assertEqual(result['items'][0]['samples'][0]['role'],'user')
            result=retrieval.search_topic(provider,'needle',role=None,path=path)
            self.assertEqual(result['matching_messages'],2)
            self.assertEqual(result['items'][0]['matching_messages'],2)
            self.assertEqual(retrieval.search_topic(provider,'absent',path=path)['matching_conversations'],0)
            self.assertEqual(retrieval.search_topic(provider,'needle',start_date='2026-01-02',path=path)['items'],[])
        with sqlite3.connect(self.paths['codex']) as connection:
            connection.execute("INSERT INTO sessions(session_id,source_path,title,file_size,file_mtime_ns,user_message_count,assistant_message_count) VALUES ('later','later','Later',0,0,4,0)")
            for seq in range(1,5):
                connection.execute("INSERT INTO messages(session_id,seq,role,create_time_iso,text) VALUES ('later',?,'user','2026-02-01T00:00:00Z','needle')",(seq,))
        first=retrieval.search_topic('codex','needle',limit=1,path=self.paths['codex'])
        self.assertEqual(first['matching_messages'],5)
        self.assertEqual(first['matching_conversations'],2)
        self.assertEqual(first['items'][0]['conversation_id'],'conv')
        self.assertTrue(first['has_more'])
        second=retrieval.search_topic('codex','needle',limit=1,offset=first['next_offset'],path=self.paths['codex'])
        self.assertEqual(second['items'][0]['conversation_id'],'later')
        self.assertEqual(len(second['items'][0]['samples']),3)
        self.assertTrue(second['items'][0]['samples_partial'])
        recent=retrieval.search_topic('codex','needle',order='recent',path=self.paths['codex'])
        self.assertEqual(recent['items'][0]['conversation_id'],'later')

    def test_historical_pages_full_text_dedup_and_readonly(self):
        digest=hashlib.sha256(self.historical_path.read_bytes()).digest()
        first=historical.search_historical('historicalneedle',limit=1,path=self.historical_path)
        self.assertTrue(first['has_more'])
        second=historical.search_historical('historicalneedle',limit=1,offset=first['next_offset'],path=self.historical_path)
        self.assertFalse(second['has_more'])
        self.assertNotEqual(first['items'][0]['document_id'],second['items'][0]['document_id'])
        self.assertEqual(historical.historical_status(self.historical_path)['documents'],2)
        doc_id=next(row['document_id'] for row in first['items']+second['items'] if row['title']=='Old')
        chunks=[]; offset=0
        while offset is not None:
            result=historical.read_historical_document(doc_id,text_offset=offset,path=self.historical_path)
            chunks.append(result['text']); offset=result['next_text_offset']
        self.assertEqual(''.join(chunks),'historicalneedle '+'x'*17000)
        self.assertEqual(historical.search_historical('absent',path=self.historical_path)['items'],[])
        with self.assertRaises(ValueError):
            historical.read_historical_document(999,path=self.historical_path)
        with self.assertRaises(sqlite3.Error):
            historical.search_historical('needle',path=self.paths['codex'])
        self.assertEqual(digest,hashlib.sha256(self.historical_path.read_bytes()).digest())
        missing=Path(self.directory.name)/'missing-historical.sqlite'
        self.assertFalse(historical.historical_status(missing)['available'])
        self.assertFalse(missing.exists())

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK not installed')
    def test_real_stdio_discovery_calls_and_errors(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        async def exercise():
            env=dict(os.environ)
            env.update(CHATGPT_HISTORICAL_DB=str(self.historical_path),CHATGPT_EXPORT_DB=str(self.paths['chatgpt']),CLAUDE_EXPORT_DB=str(self.paths['claude']),CODEX_SESSION_DB=str(self.paths['codex']),CLAUDE_CODE_SESSION_DB=str(self.paths['claude_code']))
            env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
            params=StdioServerParameters(command=sys.executable,args=['-m','conversation_archive.mcp'],env=env,cwd=tempfile.gettempdir())
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as client:
                    await client.initialize()
                    tools=(await client.list_tools()).tools
                    self.assertEqual({t.name for t in tools},{'archive_status','search_conversations','search_within_conversation','read_conversation','read_message_context','list_conversations','search_all_conversations','refresh_local_sessions','search_topic','search_historical','read_historical_document'})
                    for tool in tools:
                        self.assertTrue(tool.meta['openai/toolInvocation/invoking'])
                        self.assertTrue(tool.meta['openai/toolInvocation/invoked'])
                        self.assertLessEqual(len(tool.meta['openai/toolInvocation/invoking']), 64)
                        self.assertLessEqual(len(tool.meta['openai/toolInvocation/invoked']), 64)
                    self.assertTrue(all(t.annotations.readOnlyHint for t in tools if t.name != 'refresh_local_sessions'))
                    refresh_tool=next(t for t in tools if t.name == 'refresh_local_sessions')
                    self.assertFalse(refresh_tool.annotations.readOnlyHint)
                    self.assertTrue(refresh_tool.annotations.destructiveHint)
                    for provider in self.paths:
                        result=await client.call_tool('search_conversations',{'provider':provider,'query':'needle','limit':1})
                        self.assertFalse(result.isError)
                        self.assertTrue(result.structuredContent['has_more'])
                        self.assertEqual(result.structuredContent['search_query'], 'needle')
                        self.assertIn('1 messages returned', result.structuredContent['search_summary'])
                        self.assertIn('offset 1', result.structuredContent['search_summary'])
                        scoped=await client.call_tool('search_within_conversation',{'provider':provider,'conversation_id':'conv','query':'needle','limit':1,'include_text':False})
                        self.assertFalse(scoped.isError)
                        self.assertTrue(scoped.structuredContent['has_more'])
                        reference=scoped.structuredContent['items'][0]
                        self.assertNotIn('text',reference)
                        self.assertNotIn('needle response', scoped.structuredContent['search_summary'])
                        scoped_text=await client.call_tool('search_within_conversation',{'provider':provider,'conversation_id':'conv','query':'needle','role':'user'})
                        self.assertFalse(scoped_text.isError)
                        self.assertIn('text_offset',scoped_text.structuredContent['items'][0])
                        readback=await client.call_tool('read_conversation',{'provider':provider,'conversation_id':'conv'})
                        self.assertEqual(len(readback.structuredContent['items']),2)
                        context=await client.call_tool('read_message_context',{'provider':provider,'message_index_id':1})
                        self.assertEqual(context.structuredContent['items'][0]['next_text_offset'],4000)
                    self.assertFalse((await client.call_tool('archive_status',{})).isError)
                    browse=await client.call_tool('list_conversations',{'provider':'codex','title':'Archive'})
                    self.assertEqual(browse.structuredContent['items'][0]['message_count'],2)
                    combined=await client.call_tool('search_all_conversations',{'query':'needle','limit':1})
                    self.assertEqual(len(combined.structuredContent['providers']),4)
                    self.assertFalse(combined.structuredContent['partial'])
                    self.assertIn('claude_code: 1 messages returned', combined.structuredContent['search_summary'])
                    grouped=await client.call_tool('search_topic',{'provider':'codex','query':'needle'})
                    self.assertEqual(grouped.structuredContent['matching_messages'],1)
                    older=await client.call_tool('search_historical',{'query':'historicalneedle','limit':1})
                    self.assertTrue(older.structuredContent['has_more'])
                    doc=await client.call_tool('read_historical_document',{'document_id':older.structuredContent['items'][0]['document_id']})
                    self.assertIn('content_hash',doc.structuredContent)
                    combined_old=await client.call_tool('search_all_conversations',{'query':'historicalneedle','include_historical':True})
                    self.assertEqual(len(combined_old.structuredContent['historical']['items']),2)
                    self.assertFalse(combined_old.structuredContent['historical']['role_and_date_filters_applied'])
                    missing=await client.call_tool('search_within_conversation',{'provider':'codex','conversation_id':'missing','query':'needle'})
                    self.assertFalse(missing.isError)
                    self.assertFalse(missing.structuredContent['conversation_available'])
                    empty=await client.call_tool('search_within_conversation',{'provider':'codex','conversation_id':'conv','query':'absent'})
                    self.assertFalse(empty.isError)
                    self.assertTrue(empty.structuredContent['conversation_available'])
                    for args in ({'provider':'codex','conversation_id':'conv','query':'"'},
                                 {'provider':'codex','conversation_id':'conv','query':'needle','limit':51},
                                 {'provider':'codex','conversation_id':'conv','query':'needle','start_date':'bad'}):
                        self.assertTrue((await client.call_tool('search_within_conversation',args)).isError)
                    self.assertTrue((await client.call_tool('refresh_local_sessions',{'provider':'chatgpt'})).isError)
                    for args in ({'provider':'unknown','query':'needle'},{'provider':'codex','query':'needle','limit':51},{'provider':'codex','query':'"'}):
                        self.assertTrue((await client.call_tool('search_conversations',args)).isError)
        before={p:hashlib.sha256(path.read_bytes()).digest() for p,path in self.paths.items()}
        asyncio.run(asyncio.wait_for(exercise(),30))
        self.assertEqual(before,{p:hashlib.sha256(path.read_bytes()).digest() for p,path in self.paths.items()})
