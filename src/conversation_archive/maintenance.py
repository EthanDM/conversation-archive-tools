"""Explicit local-session refresh; no export imports, publishing, or caller-supplied paths."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from . import retrieval
from .codex import paths as codex_paths
from .codex.session_index import index_sessions as index_codex
from .codex.source_roots import local_additional_roots, protected_local_roots
from .claude_code import paths as claude_code_paths
from .claude_code.session_index import index_sessions as index_claude_code

# Serialize refreshes in this MCP process; SQLite remains responsible for other writers.
_REFRESH_LOCK = Lock()


def refresh_local_sessions(provider: str, *, include_shared: bool = False) -> dict:
    """Incrementally refresh the configured local provider and return indexer counts.

    Existing out-of-scope Codex indexes are refused. include_shared explicitly adds
    the configured shared archive alongside local and archived history, without publishing files.
    Codex retains missing local rows when active and archived roots are paired;
    other missing Codex rows are removed. Claude Code retains missing rows. No source
    files are changed. Failures may follow committed file updates; retry to finish.
    """
    if provider not in {'codex', 'claude_code'}:
        raise ValueError('Refresh supports only codex and claude_code')
    if include_shared and provider != 'codex':
        raise ValueError('include_shared supports only codex')
    shared_inputs = ()
    if include_shared:
        try:
            shared = Path(codex_paths.default_shared_input_path()).expanduser().resolve()
        except RuntimeError:
            raise ValueError('Shared Codex archive is not configured') from None
        if not shared.is_dir():
            raise ValueError('Configured shared Codex archive is unavailable')
        shared_inputs = (shared,)
    paths = codex_paths if provider == 'codex' else claude_code_paths
    source = Path(paths.default_input_path()).expanduser().resolve()
    database = Path(paths.default_db_path()).expanduser().resolve()
    if not source.exists():
        raise ValueError('Configured session source is unavailable')
    if not _REFRESH_LOCK.acquire(blocking=False):
        raise ValueError('A session refresh is already running; retry after it finishes')
    try:
        local_extra = local_additional_roots(source, database) if provider == 'codex' else ()
        additional_inputs = local_extra + shared_inputs
        protected_missing_roots = protected_local_roots(source, local_extra) if provider == 'codex' else ()
        if database.exists():
            with retrieval.open_index(provider, database) as connection:
                if provider == 'codex':
                    for row in connection.execute('SELECT source_path FROM sessions'):
                        indexed_source = Path(row[0]).resolve()
                        if not any(indexed_source == root or (root.is_dir() and root in indexed_source.parents)
                                   for root in (source, *additional_inputs)):
                            raise ValueError('Configured Codex index contains another source; use include_shared for configured shared history or refresh through the CLI')
        indexer = index_codex if provider == 'codex' else index_claude_code
        options = {'incremental': True}
        if provider == 'codex':
            options['additional_inputs'] = additional_inputs
            options['preserve_scope'] = True
            options['protected_missing_roots'] = protected_missing_roots
        indexed, skipped, _ = indexer(source, database, **options)
        # Codex's third return count describes reparsed files, whereas Claude Code's
        # describes all retained messages. Project one consistent MCP count explicitly.
        with retrieval.open_index(provider, database) as connection:
            messages = connection.execute("SELECT count(*) FROM messages").fetchone()[0]
        return {'provider': provider, 'indexed_files': indexed, 'unchanged_files': skipped,
                'retained_messages': messages, 'includes_shared': include_shared, 'completed_at': datetime.now(timezone.utc).isoformat(),
                'missing_source_policy': ('retain_local_missing_rows' if protected_missing_roots else 'remove_index_rows')
                if provider == 'codex' else 'retain_index_rows'}
    finally:
        _REFRESH_LOCK.release()
