"""Bounded reads of content-deduplicated historical documents, not role-aware message history."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time

from .chatgpt.paths import default_historical_db_path
from .retrieval import validate_page


@contextmanager
def open_historical(path: Path | None = None):
    """Validate the legacy document schema without creating or changing its database."""
    path = (path or Path(default_historical_db_path())).expanduser().resolve()
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        deadline = time.monotonic() + 5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute('SELECT id,content_hash,title,text,source_kind,source_path,format,conversation_id FROM documents LIMIT 0')
        connection.execute('SELECT rowid FROM documents_fts LIMIT 0')
        yield connection
    finally:
        connection.close()


def search_historical(query: str, *, limit: int = 10, offset: int = 0,
                      path: Path | None = None) -> dict:
    """Search older/legacy documents using FTS5, keeping indexed source and content identity.

    Content-identical documents were deduplicated by the indexer; source_path is the
    retained source, not an inventory of every duplicate snapshot. Roles/dates are
    unavailable as structured fields, so no such filters or completeness are implied.
    """
    validate_page(limit, offset)
    if not query.strip() or len(query) > 1000:
        raise ValueError('query must contain 1–1000 characters')
    with open_historical(path) as connection:
        rows = connection.execute("""SELECT d.id AS document_id,d.content_hash,d.title,
            d.source_kind,d.source_path,d.format,d.conversation_id,
            snippet(documents_fts,1,'[',']',' … ',32) AS snippet,
            bm25(documents_fts) AS score FROM documents_fts
            JOIN documents d ON d.id=documents_fts.rowid WHERE documents_fts MATCH ?
            ORDER BY score,d.id LIMIT ? OFFSET ?""", (query,limit+1,offset)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        for item in items:
            snippet = item['snippet'] or ''
            item.update(snippet=snippet[:1000], snippet_truncated=len(snippet)>1000)
        return {'scope':'indexed_historical_documents_only','items':items,'limit':limit,
                'offset':offset,'has_more':len(rows)>limit,
                'next_offset':offset+limit if len(rows)>limit else None}


def read_historical_document(document_id: int, *, text_offset: int = 0,
                             path: Path | None = None) -> dict:
    """Read 8000-character chunks by indexed ID; never open a returned source path."""
    if type(document_id) is not int or document_id < 1 or type(text_offset) is not int or text_offset < 0:
        raise ValueError('Invalid document ID or text offset')
    with open_historical(path) as connection:
        row = connection.execute("""SELECT id AS document_id,content_hash,title,
            source_kind,source_path,format,conversation_id,length(text) AS text_length,
            substr(text,?,8000) AS text FROM documents WHERE id=?""",
            (text_offset+1,document_id)).fetchone()
        if row is None:
            raise ValueError('Historical document not found')
        result = dict(row)
        more = text_offset+8000 < result['text_length']
        result.update(text_offset=text_offset,truncated=text_offset>0 or more,
                      next_text_offset=text_offset+8000 if more else None)
        return result


def historical_status(path: Path | None = None) -> dict:
    """Report retained document counts; indexed sources may overlap newer message history."""
    try:
        with open_historical(path) as connection:
            count = connection.execute('SELECT count(*) FROM documents').fetchone()[0]
            kinds = [dict(row) for row in connection.execute('SELECT source_kind,count(*) AS documents FROM documents GROUP BY source_kind ORDER BY source_kind')]
            return {'available':True,'documents':count,'source_kinds':kinds,
                    'scope':'indexed_historical_documents_only','last_refresh':None}
    except (OSError,sqlite3.Error,ValueError):
        return {'available':False,'error':'Historical index unavailable or incompatible'}
