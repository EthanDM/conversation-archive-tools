"""Bounded, structured reads of existing provider indexes; never refresh or create them."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path
import sqlite3
import json
import time

from .chatgpt import paths as chatgpt_paths
from .claude import paths as claude_paths
from .codex import paths as codex_paths
from .claude_code import paths as claude_code_paths

PROVIDERS = {
    "chatgpt": chatgpt_paths.default_db_path,
    "claude": claude_paths.default_db_path,
    "codex": codex_paths.default_db_path,
    "claude_code": claude_code_paths.default_db_path,
}


@contextmanager
def open_index(provider: str, path: Path | None = None):
    """Open one validated index read-only, with a consistent snapshot for each request."""
    if provider not in PROVIDERS:
        raise ValueError("Unknown archive provider")
    path = (path or Path(PROVIDERS[provider]())).expanduser().resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        deadline = time.monotonic() + 5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        metadata = dict(connection.execute("SELECT key, value FROM index_meta"))
        if provider in {"claude", "claude_code"}:
            if metadata.get("provider") != provider or metadata.get("schema_version") != "1":
                raise ValueError("Wrong provider or unsupported schema")
        elif provider == "codex":
            if metadata.get("provider") not in {None, "codex"} or "indexer_version" not in metadata:
                raise ValueError("Wrong provider or unsupported schema")
        elif metadata.get("provider") not in {None, "chatgpt"} or not any(
            key in metadata for key in ("indexer_version", "indexer_version_applied")
        ):
            raise ValueError("Wrong provider or unsupported schema")
        # Older ChatGPT/Codex indexes lack provider tags; validate their distinct columns.
        projection(provider, connection)
        yield connection
    finally:
        connection.close()


def projection(provider: str, connection: sqlite3.Connection) -> str:
    """Normalize identifiers while retaining provider-specific provenance and branch fields."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
    if provider == "claude":
        table, key, foreign, time, source = "conversations", "uuid", "conversation_uuid", "created_at", "c.source"
        message, parent, branch = "m.uuid", "m.parent_uuid", "NULL"
    elif provider == "chatgpt":
        table, key, foreign, time, source = "conversations", "conversation_id", "conversation_id", "create_time_iso", "NULL"
        message, parent, branch = "m.message_id", "NULL", "NULL"
    else:
        table, key, foreign, time, source = "sessions", "session_id", "session_id", "create_time_iso", "c.source_path"
        message = "m.uuid" if provider == "claude_code" else "NULL"
        parent = "m.parent_uuid" if provider == "claude_code" else "NULL"
        branch = "m.is_sidechain" if provider == "claude_code" else "NULL"
    required = {"id", "seq", "role", "text", foreign, time}
    if not required <= columns or (provider == "codex" and "uuid" in columns):
        raise ValueError("Wrong provider or unsupported schema")
    sql = f"""SELECT m.id AS message_index_id, m.{foreign} AS conversation_id,
        m.seq, m.role, m.{time} AS timestamp, m.text, c.title,
        {source} AS source, {message} AS message_id, {parent} AS parent_id,
        {branch} AS is_sidechain,
        {"m.node_id" if provider == "chatgpt" else "NULL"} AS node_id,
        {"c.cwd" if provider in {"codex", "claude_code"} else "NULL"} AS cwd FROM messages m JOIN {table} c ON c.{key}=m.{foreign}"""
    connection.execute(sql + " LIMIT 0")
    connection.execute("SELECT rowid FROM messages_fts LIMIT 0")
    return sql


def validate_page(limit: int, offset: int) -> None:
    """Reject oversized or invalid pages before querying."""
    if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError("limit must be 1–50 and offset 0–100000")


def page_result(rows, limit: int, offset: int, max_chars: int) -> dict:
    """Use one lookahead row; per-message truncation is explicit and recoverable by chunks."""
    items = []
    for row in rows[:limit]:
        item = dict(row)
        text = item.pop("text") or ""
        item.update(text=text[:max_chars], text_length=len(text), truncated=len(text) > max_chars)
        items.append(item)
    return {"items": items, "limit": limit, "offset": offset,
            "has_more": len(rows) > limit, "next_offset": offset + limit if len(rows) > limit else None}


def search_conversations(provider: str, query: str, *, role: str | None = None,
                         start_date: str | None = None, end_date: str | None = None,
                         limit: int = 10, offset: int = 0, path: Path | None = None) -> dict:
    """Search indexed message text or titles using FTS5 syntax and inclusive timestamp dates.

    Missing timestamps cannot match date filters. Results are newest first within this provider; title matches use literal query text.
    """
    validate_page(limit, offset)
    predicate, args = search_predicate(query, role, start_date, end_date)
    with open_index(provider, path) as connection:
        sql = f"SELECT n.* FROM ({projection(provider, connection)}) n WHERE {predicate}"
        rows = connection.execute(sql + " ORDER BY timestamp DESC, message_index_id DESC LIMIT ? OFFSET ?", args + [limit + 1, offset]).fetchall()
        return {"provider": provider, "scope": "indexed_messages_only", **page_result(rows, limit, offset, 1000)}


def read_conversation(provider: str, conversation_id: str, *, limit: int = 20,
                      offset: int = 0, path: Path | None = None) -> dict:
    """Read messages in indexed order, preserving all retained branches; no branch is inferred."""
    validate_page(limit, offset)
    if not conversation_id or len(conversation_id) > 200:
        raise ValueError("conversation_id must contain 1–200 characters")
    with open_index(provider, path) as connection:
        sql = projection(provider, connection)
        if not connection.execute(f"SELECT 1 FROM ({sql}) WHERE conversation_id=? LIMIT 1", (conversation_id,)).fetchone():
            raise ValueError("Conversation not found")
        rows = connection.execute(f"SELECT * FROM ({sql}) WHERE conversation_id=? ORDER BY seq, message_index_id LIMIT ? OFFSET ?", (conversation_id, limit + 1, offset)).fetchall()
        return {"provider": provider, **page_result(rows, limit, offset, 2000)}


def read_message_context(provider: str, message_index_id: int, *, before: int = 2,
                         after: int = 2, text_offset: int = 0, path: Path | None = None) -> dict:
    """Read adjacent messages and a 4000-character chunk of the hit, allowing full recovery."""
    if type(message_index_id) is not int or message_index_id < 1 or any(type(n) is not int or not 0 <= n <= 10 for n in (before, after)):
        raise ValueError("Invalid message ID or context bounds (0–10)")
    if type(text_offset) is not int or text_offset < 0:
        raise ValueError("text_offset must be nonnegative")
    with open_index(provider, path) as connection:
        sql = projection(provider, connection)
        hit = connection.execute(f"SELECT * FROM ({sql}) WHERE message_index_id=?", (message_index_id,)).fetchone()
        if hit is None:
            raise ValueError("Message not found")
        rows = connection.execute(f"SELECT * FROM ({sql}) WHERE conversation_id=? AND seq BETWEEN ? AND ? ORDER BY seq, message_index_id", (hit['conversation_id'], hit['seq'] - before, hit['seq'] + after)).fetchall()
        result = page_result(rows, 21, 0, 1000)
        for item in result["items"]:
            if item["message_index_id"] == message_index_id:
                text = hit['text'] or ""
                item.update(text=text[text_offset:text_offset + 4000], text_offset=text_offset,
                            truncated=text_offset > 0 or text_offset + 4000 < len(text),
                            next_text_offset=text_offset + 4000 if text_offset + 4000 < len(text) else None)
        return {"provider": provider, **result}


def archive_status(paths: dict[str, Path] | None = None) -> dict:
    """Report indexed coverage, not account completeness; filesystem mtime is not refresh time."""
    results = []
    for provider, default in PROVIDERS.items():
        path = Path(paths[provider] if paths else default()).expanduser()
        try:
            with open_index(provider, path) as connection:
                sql = projection(provider, connection)
                counts = dict(connection.execute(f"SELECT count(*) AS messages, count(DISTINCT conversation_id) AS conversations, min(timestamp) AS first_message, max(timestamp) AS last_message FROM ({sql})").fetchone())
                metadata = dict(connection.execute("SELECT key,value FROM index_meta"))
                results.append({"provider": provider, "available": True, **counts,
                                "index_file_modified_unix": path.stat().st_mtime,
                                "last_refresh": metadata.get("last_refresh_completed_at"),
                                "source_root": metadata.get("source_root"),
                                "source_roots": json.loads(metadata.get("source_roots", "[]")),
                                "freshness_scope": "last_completed_indexing_run; source_changes_not_checked",
                                "requires_refresh": provider == "codex" and metadata.get("indexer_version") != "2"})
        except (OSError, sqlite3.Error, ValueError):
            results.append({"provider": provider, "available": False, "error": "Index unavailable or incompatible"})
    from .historical import historical_status
    return {"scope": "indexed_messages_only", "providers": results,
            "historical": historical_status()}


def date_filters(start_date: str | None, end_date: str | None) -> None:
    """Validate inclusive source-calendar dates before opening any database."""
    for value in (start_date, end_date):
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("Dates must use YYYY-MM-DD")
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must precede end_date")


def literal_pattern(value: str) -> str:
    """Build a substring pattern without treating user punctuation as SQL wildcards."""
    if len(value) > 1000:
        raise ValueError("Filter text must be at most 1000 characters")
    return '%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def list_conversations(provider: str, *, title: str = '', cwd: str | None = None,
                       start_date: str | None = None, end_date: str | None = None,
                       limit: int = 20, offset: int = 0, path: Path | None = None) -> dict:
    """Browse conversation metadata, newest retained message first, without returning text.

    Dates filter the last retained message date. cwd is a literal directory substring
    for session providers only. Empty conversations are included with null message dates.
    """
    validate_page(limit, offset)
    date_filters(start_date, end_date)
    if cwd is not None and provider not in {'codex', 'claude_code'}:
        raise ValueError('cwd is supported only for session providers')
    with open_index(provider, path) as connection:
        normalized = projection(provider, connection)
        if provider == 'claude':
            table, key, source, directory = 'conversations', 'uuid', 'c.source', 'NULL'
        elif provider == 'chatgpt':
            table, key, source, directory = 'conversations', 'conversation_id', 'NULL', 'NULL'
        else:
            table, key, source, directory = 'sessions', 'session_id', 'c.source_path', 'c.cwd'
        sql = f"""SELECT c.{key} AS conversation_id, c.title, {source} AS source,
            {directory} AS cwd, count(n.message_index_id) AS message_count,
            min(n.timestamp) AS first_message, max(n.timestamp) AS last_message
            FROM {table} c LEFT JOIN ({normalized}) n ON n.conversation_id=c.{key}
            WHERE coalesce(c.title, '') LIKE ? ESCAPE '\\'"""
        args = [literal_pattern(title)]
        if cwd is not None:
            sql += " AND c.cwd LIKE ? ESCAPE '\\'"
            args.append(literal_pattern(cwd))
        sql += f' GROUP BY c.{key} HAVING 1=1'
        for condition, value in (('substr(max(n.timestamp),1,10) >= ?', start_date), ('substr(max(n.timestamp),1,10) <= ?', end_date)):
            if value is not None:
                sql += ' AND ' + condition
                args.append(value)
        rows = connection.execute(sql + ' ORDER BY last_message DESC, conversation_id LIMIT ? OFFSET ?', args + [limit + 1, offset]).fetchall()
        return {'provider': provider, 'items': [dict(row) for row in rows[:limit]],
                'limit': limit, 'offset': offset, 'has_more': len(rows) > limit,
                'next_offset': offset + limit if len(rows) > limit else None}


def search_all_conversations(query: str, *, role: str | None = None,
                             start_date: str | None = None, end_date: str | None = None,
                             limit: int = 10, offset: int = 0,
                             paths: dict[str, Path] | None = None,
                             include_historical: bool = False) -> dict:
    """Search all four providers with independent pages, preserving per-provider continuations.

    No global rank or deduplication is inferred. Partial failures are explicit and do
    not suppress matches from available indexes. Each provider has its own snapshot.
    """
    validate_page(limit, offset)
    date_filters(start_date, end_date)
    if not query.strip() or len(query) > 1000 or role not in {None, 'user', 'assistant'}:
        raise ValueError('Invalid query or role')
    results = []
    for provider in PROVIDERS:
        try:
            result = search_conversations(provider, query, role=role, start_date=start_date,
                                          end_date=end_date, limit=limit, offset=offset,
                                          path=paths[provider] if paths else None)
            results.append({'available': True, **result})
        except (OSError, sqlite3.Error, ValueError):
            results.append({'provider': provider, 'available': False,
                            'error': 'Index unavailable, incompatible, or invalid FTS query'})
    response = {'scope': 'indexed_messages_only', 'pagination': 'per_provider',
                'partial': any(not result['available'] for result in results), 'providers': results}
    if include_historical:
        from .historical import search_historical
        try:
            response['historical'] = {'available': True, **search_historical(query, limit=limit, offset=offset),
                                      'role_and_date_filters_applied': False}
        except (OSError, sqlite3.Error, ValueError):
            response['historical'] = {'available': False, 'error': 'Historical query unavailable or invalid'}
            response['partial'] = True
    return response


def search_predicate(query: str, role: str | None, start_date: str | None,
                     end_date: str | None) -> tuple[str, list]:
    """Share exact match/filter semantics between message search and grouped topic search."""
    if not query.strip() or len(query) > 1000:
        raise ValueError("query must contain 1–1000 characters")
    if role not in {None, "user", "assistant"}:
        raise ValueError("role must be user or assistant")
    date_filters(start_date, end_date)
    predicate = "(message_index_id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?) OR title LIKE ? ESCAPE '\\')"
    args = [query, literal_pattern(query)]
    for condition, value in (("role = ?", role), ("substr(timestamp,1,10) >= ?", start_date), ("substr(timestamp,1,10) <= ?", end_date)):
        if value is not None:
            predicate += " AND " + condition
            args.append(value)
    return predicate, args


def search_topic(provider: str, query: str, *, role: str | None = "user",
                 start_date: str | None = None, end_date: str | None = None,
                 order: str = "chronological", limit: int = 10, offset: int = 0,
                 path: Path | None = None) -> dict:
    """Group all matching indexed messages by conversation, with exact scoped counts.

    Default evidence is user text; pass role=None for both roles. Each group includes
    at most three 1000-character samples. Counts cover the entire matching set, not
    just the page. Chronology uses the first match; null dates sort last.
    """
    validate_page(limit, offset)
    if order not in {"chronological", "recent"}:
        raise ValueError("order must be chronological or recent")
    predicate, args = search_predicate(query, role, start_date, end_date)
    with open_index(provider, path) as connection:
        matched = f"SELECT * FROM ({projection(provider, connection)}) WHERE {predicate}"
        counts = dict(connection.execute(f"SELECT count(*) AS matching_messages,count(DISTINCT conversation_id) AS matching_conversations FROM ({matched})", args).fetchone())
        sort = "first_match ASC" if order == "chronological" else "last_match DESC"
        rows = connection.execute(f"""SELECT conversation_id,title,source,cwd,
            count(*) AS matching_messages,min(timestamp) AS first_match,max(timestamp) AS last_match
            FROM ({matched}) GROUP BY conversation_id
            ORDER BY first_match IS NULL,{sort},conversation_id LIMIT ? OFFSET ?""", args+[limit+1,offset]).fetchall()
        items=[]
        for row in rows[:limit]:
            item=dict(row)
            samples=connection.execute(f"SELECT * FROM ({matched}) WHERE conversation_id=? ORDER BY timestamp IS NULL,timestamp,seq,message_index_id LIMIT 3", args+[row['conversation_id']]).fetchall()
            item['samples']=page_result(samples,3,0,1000)['items']
            item['samples_partial']=row['matching_messages']>3
            items.append(item)
        return {'provider':provider,'scope':'indexed_messages_matching_query_only',
                'role':role,'order':order,**counts,'items':items,'limit':limit,'offset':offset,
                'has_more':len(rows)>limit,'next_offset':offset+limit if len(rows)>limit else None}
