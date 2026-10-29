"""Select configured Codex source roots without silently narrowing an index."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .paths import DEFAULT_INPUT_PATH, default_shared_input_path


def indexed_roots(database: Path) -> set[Path]:
    """Return the index's last completed source roots, without creating the database."""
    if not database.exists():
        return set()
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return _roots_from_connection(connection)


def _roots_from_connection(connection: sqlite3.Connection) -> set[Path]:
    metadata = dict(connection.execute("SELECT key, value FROM index_meta"))
    if "source_roots" not in metadata:
        value = [metadata["source_root"]] if metadata.get("source_root") else []
    else:
        value = json.loads(metadata["source_roots"])
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            raise ValueError("Codex index has invalid source roots")
    return {Path(item).expanduser().resolve() for item in value}


def local_archived_root(source: Path) -> Path | None:
    """Return the configured local archive companion, if applicable."""
    configured = os.environ.get("CODEX_SESSION_ARCHIVED_INPUT")
    if configured:
        return Path(configured).expanduser().resolve()
    if source.expanduser().resolve() == DEFAULT_INPUT_PATH.resolve():
        return (DEFAULT_INPUT_PATH.parent / "archived_sessions").resolve()
    return None


def protected_local_roots(source: Path, additional: tuple[Path, ...]) -> tuple[Path, ...]:
    """Retain missing local rows only when active and archived roots are selected together."""
    archive = local_archived_root(source)
    if archive is None or archive == source.resolve() or archive not in additional:
        return ()
    return (source.resolve(), archive)


def local_additional_roots(
    source: Path, database: Path, *, preserve_shared: bool = False, reset: bool = False,
) -> tuple[Path, ...]:
    """Add archived sessions and optionally preserve a previously indexed shared root.

    Missing previously indexed roots remain selected so index validation fails before
    pruning. An absent, never-indexed default archive is optional on new machines.
    Custom local roots have no implicit companion unless explicitly configured. A
    reset can recover an unreadable database but cannot infer its prior roots.
    """
    source = source.expanduser().resolve()
    try:
        previous = indexed_roots(database)
    except (sqlite3.DatabaseError, ValueError) as error:
        if not reset:
            raise
        if preserve_shared:
            raise ValueError(
                "Cannot infer previous Codex source scope; choose an explicit reset scope"
            ) from error
        previous = set()
    configured = os.environ.get("CODEX_SESSION_ARCHIVED_INPUT")
    archive = local_archived_root(source)
    roots: list[Path] = []
    if archive is not None and (configured or archive.exists() or archive in previous):
        if archive != source:
            roots.append(archive)
    if preserve_shared:
        try:
            shared = Path(default_shared_input_path()).expanduser().resolve()
        except RuntimeError:
            shared = None
        if shared is not None and shared in previous and shared != source and shared not in roots:
            roots.append(shared)
        known = {source, *roots}
        if previous - known:
            raise ValueError("Codex index has other source roots; choose an explicit refresh scope")
    return tuple(roots)


def ensure_scope_not_narrowed(connection: sqlite3.Connection, roots: tuple[Path, ...]) -> None:
    """Reject narrowing using metadata protected by the caller's write transaction."""
    selected = tuple(root.expanduser().resolve() for root in roots)
    previous = _roots_from_connection(connection)
    if any(not any(old == root or root in old.parents for root in selected) for old in previous):
        raise ValueError("Codex index has other source roots; use --reset or a separate database")
