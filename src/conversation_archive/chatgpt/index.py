#!/usr/bin/env python3
"""Build a local SQLite FTS index from official ChatGPT conversation exports.

The index stores conversation metadata and extracted message text only on the
local machine. It accepts extracted exports, shard sets, and export ZIPs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import re
import sqlite3
import sys
import tempfile
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .paths import default_db_path, default_export_path

INDEXER_VERSION = "5"
INDEX_MODE_ALL_NODES = "all_nodes"
INDEX_MODE_CURRENT_PATH_ONLY = "current_path_only"
SHARDED_EXPORT_GLOB = "conversations-*.json"


def _utc_iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    try:
        return dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc).isoformat()
    except Exception:
        return None


def _utc_iso_from_float(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()
    except Exception:
        return None


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _build_attachment_index(message: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    metadata = message.get("metadata") or {}
    attachments = metadata.get("attachments") or []
    attachment_index: Dict[str, Dict[str, Any]] = {}
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_id = attachment.get("id")
        if isinstance(attachment_id, str) and attachment_id:
            attachment_index[attachment_id] = attachment
    return attachment_index


def _attachment_label(
    attachment_index: Dict[str, Dict[str, Any]],
    *,
    asset_pointer: Optional[str] = None,
    attachment: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    attachment_name: Optional[str] = None
    asset_id: Optional[str] = None

    if isinstance(asset_pointer, str) and asset_pointer:
        asset_id = asset_pointer.removeprefix("file-service://")
        attachment = attachment or attachment_index.get(asset_id)

    if isinstance(attachment, dict):
        name = attachment.get("name")
        attachment_name = name if isinstance(name, str) and name else None
        if asset_id is None:
            attachment_id = attachment.get("id")
            asset_id = attachment_id if isinstance(attachment_id, str) and attachment_id else None

    label = attachment_name or asset_id
    if not label:
        return None
    return f"[Attachment: {label}]"


def _extract_message_text(message: Dict[str, Any]) -> Tuple[Optional[str], str]:
    content = message.get("content") or {}
    content_type = content.get("content_type")
    attachment_index = _build_attachment_index(message)

    if content_type in (None, "text", "multimodal_text"):
        parts = content.get("parts") or []
        out_parts: List[str] = []
        seen_attachment_labels = set()
        for p in parts:
            if isinstance(p, str):
                if p:
                    out_parts.append(p)
                continue

            if not isinstance(p, dict):
                coerced = _coerce_text(p).strip()
                if coerced:
                    out_parts.append(coerced)
                continue

            part_type = p.get("content_type")
            if part_type == "audio_transcription":
                transcript = _coerce_text(p.get("text")).strip()
                if transcript:
                    out_parts.append(f"[Transcript] {transcript}")
                continue

            if part_type == "image_asset_pointer" or isinstance(p.get("asset_pointer"), str):
                label = _attachment_label(
                    attachment_index,
                    asset_pointer=p.get("asset_pointer"),
                )
                if label and label not in seen_attachment_labels:
                    out_parts.append(label)
                    seen_attachment_labels.add(label)
                continue

            text = p.get("text")
            if isinstance(text, str) and text.strip():
                out_parts.append(text.strip())
                continue

            coerced = _coerce_text(p).strip()
            if coerced:
                out_parts.append(coerced)

        for attachment in attachment_index.values():
            label = _attachment_label(attachment_index, attachment=attachment)
            if label and label not in seen_attachment_labels:
                out_parts.append(label)
                seen_attachment_labels.add(label)
        return content_type, "\n".join(out_parts).strip()

    if content_type == "code":
        text = _coerce_text(content.get("text")).strip()
        language = _coerce_text(content.get("language")).strip()
        if text and language and language != "unknown":
            return content_type, f"[code:{language}]\n{text}"
        if text:
            return content_type, text
        text = language
        return content_type, text.strip()

    if content_type in {"execution_output", "system_error", "sonic_webpage", "tether_quote"}:
        text = _coerce_text(content.get("text")).strip()
        return content_type, text

    if content_type == "tether_browsing_display":
        text = _coerce_text(content.get("result") or content.get("summary")).strip()
        return content_type, text

    if content_type == "reasoning_recap":
        text = _coerce_text(content.get("content")).strip()
        return content_type, text

    if content_type == "user_editable_context":
        user_profile = content.get("user_profile")
        if user_profile:
            return content_type, _coerce_text(user_profile).strip()
        return content_type, _coerce_text(content).strip()

    if content_type == "app_pairing_content":
        out_parts: List[str] = []
        custom_instructions = _coerce_text(content.get("custom_instructions")).strip()
        if custom_instructions:
            out_parts.append(custom_instructions)
        for part in content.get("context_parts") or []:
            if not isinstance(part, dict):
                coerced = _coerce_text(part).strip()
                if coerced:
                    out_parts.append(coerced)
                continue
            text = _coerce_text(part.get("text")).strip()
            if text:
                out_parts.append(text)
        return content_type, "\n\n".join(out_parts).strip()

    # Fallback: stringify.
    return content_type, _coerce_text(content).strip()


def _is_hidden(message: Dict[str, Any], include_hidden: bool) -> bool:
    if include_hidden:
        return False
    meta = message.get("metadata") or {}
    if meta.get("is_visually_hidden_from_conversation") is True:
        return True
    content = message.get("content") or {}
    content_type = content.get("content_type")
    if content_type == "user_editable_context":
        return True
    # Internal / tool-related message payloads that are typically not useful for search/browsing.
    # (You can still index them with --include-hidden.)
    if content_type in {
        "app_pairing_content",
        "reasoning_recap",
        "thoughts",
        "tool",
        "tool_result",
        "search_query",
    }:
        return True
    return False


def _linearize_current_path(
    mapping: Dict[str, Any], current_node: Optional[str]
) -> List[str]:
    if not current_node:
        return []
    path: List[str] = []
    node_id = current_node
    seen = set()
    while node_id and node_id not in seen:
        seen.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            break
        path.append(node_id)
        parent = node.get("parent")
        node_id = parent if isinstance(parent, str) else None
    path.reverse()
    return path


def _iter_messages_all_nodes(
    mapping: Dict[str, Any], *, include_hidden: bool
) -> List[Tuple[Optional[float], str, Dict[str, Any]]]:
    out: List[Tuple[Optional[float], str, Dict[str, Any]]] = []
    for node_id, node in mapping.items():
        if not isinstance(node_id, str) or not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        if _is_hidden(message, include_hidden=include_hidden):
            continue
        create_time = message.get("create_time")
        create_time_f: Optional[float] = None
        if isinstance(create_time, (int, float)):
            create_time_f = float(create_time)
        out.append((create_time_f, node_id, message))

    # Sort by create_time where available; keep stable order for missing timestamps.
    out.sort(key=lambda t: (t[0] is None, t[0] if t[0] is not None else 0.0, t[1]))
    return out


def _iter_json_array(fp) -> Iterator[Any]:
    """Yield top-level JSON-array values without loading the complete export.

    ``fp`` must contain one JSON array. Invalid or truncated input raises
    ``ValueError`` rather than silently producing a partial index.
    """
    decoder = json.JSONDecoder()
    buf = ""

    def _read_more() -> bool:
        nonlocal buf
        chunk = fp.read(1024 * 1024)
        if not chunk:
            return False
        buf += chunk
        return True

    # Find the '['
    while True:
        if not _read_more():
            raise ValueError("unexpected EOF before JSON array")
        idx = buf.find("[")
        if idx != -1:
            buf = buf[idx + 1 :]
            break
        if len(buf) > 1024 * 1024:
            buf = buf[-1024 * 1024 :]

    while True:
        buf = buf.lstrip()
        if not buf:
            if not _read_more():
                raise ValueError("unexpected EOF inside JSON array")
            continue
        if buf[0] == "]":
            return
        try:
            obj, end = decoder.raw_decode(buf)
        except json.JSONDecodeError:
            if not _read_more():
                raise ValueError("unexpected EOF decoding array element")
            continue
        yield obj
        buf = buf[end:].lstrip()
        if buf.startswith(","):
            buf = buf[1:]


def _resolve_input_paths(input_path: Path) -> List[Path]:
    if input_path.is_dir():
        shard_paths = sorted(input_path.glob(SHARDED_EXPORT_GLOB))
        if shard_paths:
            return shard_paths

        single_export = input_path / "conversations.json"
        if single_export.exists():
            return [single_export]

        raise FileNotFoundError(
            f"no {SHARDED_EXPORT_GLOB} shards or conversations.json found under {input_path}"
        )

    if input_path.is_file():
        return [input_path]

    glob_matches = sorted(Path(match) for match in glob.glob(str(input_path)))
    if glob_matches:
        return glob_matches

    raise FileNotFoundError(str(input_path))


def _extract_zip_export(input_path: Path, destination: Path) -> None:
    """Copy only recognized conversation JSON members from a ZIP into ``destination``.

    The caller owns the temporary destination. Archive member paths are reduced
    to filenames, preventing the ZIP from choosing any output location.
    """
    with zipfile.ZipFile(input_path) as archive:
        files = [item for item in archive.infolist() if not item.is_dir()]
        shards = [item for item in files if Path(item.filename).name.startswith("conversations-") and Path(item.filename).name.endswith(".json")]
        selected = sorted(shards or [item for item in files if Path(item.filename).name == "conversations.json"], key=lambda item: item.filename)
        if not selected:
            raise FileNotFoundError(f"no {SHARDED_EXPORT_GLOB} shards or conversations.json found in {input_path}")
        for item in selected:
            name = Path(item.filename).name
            target = destination / name
            if target.exists():
                raise ValueError(f"duplicate export member name: {name}")
            with archive.open(item) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS index_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY,
  title TEXT,
  create_time REAL,
  update_time REAL,
  create_time_iso TEXT,
  update_time_iso TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  node_id TEXT NOT NULL,
  message_id TEXT,
  role TEXT,
  author_name TEXT,
  create_time REAL,
  create_time_iso TEXT,
  content_type TEXT,
  text TEXT,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_conv_time
  ON messages(conversation_id, create_time, id);

CREATE INDEX IF NOT EXISTS idx_messages_conv_seq
  ON messages(conversation_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text,
  conversation_id UNINDEXED,
  title,
  role UNINDEXED,
  create_time_iso UNINDEXED,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text, conversation_id, title, role, create_time_iso)
  VALUES (
    new.id,
    new.text,
    new.conversation_id,
    (SELECT title FROM conversations WHERE conversation_id=new.conversation_id),
    new.role,
    new.create_time_iso
  );
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  DELETE FROM messages_fts WHERE rowid=old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  DELETE FROM messages_fts WHERE rowid=old.id;
  INSERT INTO messages_fts(rowid, text, conversation_id, title, role, create_time_iso)
  VALUES (
    new.id,
    new.text,
    new.conversation_id,
    (SELECT title FROM conversations WHERE conversation_id=new.conversation_id),
    new.role,
    new.create_time_iso
  );
END;
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON;")
    fts_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    if fts_row and "content='messages'" in (fts_row[0] or ""):
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS messages_ai;
            DROP TRIGGER IF EXISTS messages_ad;
            DROP TRIGGER IF EXISTS messages_au;
            DROP TABLE messages_fts;
            """
        )
    conn.executescript(SCHEMA_SQL)
    if fts_row and "content='messages'" in (fts_row[0] or ""):
        conn.execute(
            """
            INSERT INTO messages_fts(rowid, text, conversation_id, title, role, create_time_iso)
            SELECT m.id, m.text, m.conversation_id, c.title, m.role, m.create_time_iso
            FROM messages m JOIN conversations c ON c.conversation_id=m.conversation_id
            """
        )
        conn.commit()
    return conn


def _normalize_title(title: str) -> str:
    title = title.strip() or "Untitled"
    title = re.sub(r"[\\/:*?\"<>|]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title[:160]


def _write_markdown(
    out_dir: Path,
    conversation_id: str,
    title: str,
    rows: List[Tuple[str, Optional[str], str]],
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_title = _normalize_title(title)
    md_path = out_dir / f"{conversation_id} - {safe_title}.md"
    with md_path.open("w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(f"- conversation_id: `{conversation_id}`\n\n")
        for role, ts, text in rows:
            ts_line = f" ({ts})" if ts else ""
            f.write(f"## {role}{ts_line}\n\n")
            f.write(text.strip() + "\n\n")
    return md_path


def index_export(
    input_path: Path,
    db_path: Path,
    *,
    include_hidden: bool,
    write_markdown_dir: Optional[Path],
    limit: Optional[int],
    progress_every: int,
    reset: bool,
    all_nodes: bool,
    incremental: bool,
) -> None:
    """Index an official ChatGPT export into ``db_path``.

    ``input_path`` may be a ZIP, directory, JSON file, or shard glob. ``reset``
    replaces the target database; incremental mode preserves unchanged
    conversations only when both index behavior and update timestamps match.
    Optional Markdown output is a separate local copy of indexed transcripts.
    """
    if input_path.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="conversation-archive-export-") as temporary:
            temporary_path = Path(temporary)
            _extract_zip_export(input_path, temporary_path)
            return index_export(temporary_path, db_path, include_hidden=include_hidden, write_markdown_dir=write_markdown_dir, limit=limit, progress_every=progress_every, reset=reset, all_nodes=all_nodes, incremental=incremental)
    input_paths = _resolve_input_paths(input_path)

    if reset and db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            try:
                (db_path.parent / (db_path.name + suffix)).unlink()
            except FileNotFoundError:
                pass
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = _connect(db_path)
    cur = conn.cursor()

    requested_mode = INDEX_MODE_ALL_NODES if all_nodes else INDEX_MODE_CURRENT_PATH_ONLY

    # If the indexer behavior changed since the DB was created, disable skipping so we refresh data.
    try:
        prev_mode_row = cur.execute(
            "SELECT value FROM index_meta WHERE key='index_mode_applied'"
        ).fetchone()
        prev_mode = prev_mode_row[0] if prev_mode_row else None

        prev_version_row = cur.execute(
            "SELECT value FROM index_meta WHERE key='indexer_version_applied'"
        ).fetchone()
        prev_version = prev_version_row[0] if prev_version_row else None

        # Back-compat: older runs wrote only indexer_version.
        if prev_version is None:
            legacy = cur.execute(
                "SELECT value FROM index_meta WHERE key='indexer_version'"
            ).fetchone()
            prev_version = legacy[0] if legacy else None
    except Exception:
        prev_mode = None
        prev_version = None

    if incremental and prev_mode != requested_mode:
        print(
            f"index mode changed ({prev_mode or 'unknown'} -> {requested_mode}); reindexing all conversations",
            file=sys.stderr,
        )
        incremental = False

    if incremental and str(prev_version or "") != INDEXER_VERSION:
        print(
            f"indexer version changed ({prev_version or 'unknown'} -> {INDEXER_VERSION}); reindexing all conversations",
            file=sys.stderr,
        )
        incremental = False

    processed = 0
    inserted_messages = 0
    reindexed_conversations = 0

    for source_path in input_paths:
        with source_path.open("r", encoding="utf-8") as fp:
            for conv in _iter_json_array(fp):
                if limit is not None and processed >= limit:
                    break

                if not isinstance(conv, dict):
                    continue

                conversation_id = conv.get("conversation_id") or conv.get("id")
                if not isinstance(conversation_id, str) or not conversation_id:
                    continue

                title = conv.get("title") or ""
                if not isinstance(title, str):
                    title = _coerce_text(title)
                create_time = conv.get("create_time")
                update_time = conv.get("update_time")

                update_time_f: Optional[float] = (
                    float(update_time) if isinstance(update_time, (int, float)) else None
                )

                if incremental:
                    existing = cur.execute(
                        "SELECT update_time FROM conversations WHERE conversation_id=?",
                        (conversation_id,),
                    ).fetchone()
                    if existing is not None:
                        existing_update_time = existing[0]
                        if (
                            existing_update_time is not None
                            and update_time_f is not None
                            and float(existing_update_time) == update_time_f
                        ):
                            processed += 1
                            if progress_every > 0 and processed % progress_every == 0:
                                conn.commit()
                                print(
                                    f"indexed {processed} conversations, {inserted_messages} messages...",
                                    file=sys.stderr,
                                )
                            continue

                cur.execute(
                    """
                    INSERT INTO conversations(conversation_id, title, create_time, update_time, create_time_iso, update_time_iso)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                      title=excluded.title,
                      create_time=excluded.create_time,
                      update_time=excluded.update_time,
                      create_time_iso=excluded.create_time_iso,
                      update_time_iso=excluded.update_time_iso
                    """,
                    (
                        conversation_id,
                        title,
                        float(create_time) if isinstance(create_time, (int, float)) else None,
                        update_time_f,
                        _utc_iso(create_time),
                        _utc_iso(update_time),
                    ),
                )

                # Replace messages for this conversation (needed for incremental updates and to avoid duplicates).
                cur.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
                reindexed_conversations += 1

                mapping = conv.get("mapping") or {}
                if not isinstance(mapping, dict):
                    mapping = {}
                md_rows: List[Tuple[str, Optional[str], str]] = []
                seq = 0

                if all_nodes:
                    items = _iter_messages_all_nodes(mapping, include_hidden=include_hidden)
                    iterable: Iterable[Tuple[Optional[float], str, Dict[str, Any]]] = items
                else:
                    current_node = conv.get("current_node")
                    current_node = current_node if isinstance(current_node, str) else None
                    node_path = _linearize_current_path(mapping, current_node)
                    iterable = []
                    tmp: List[Tuple[Optional[float], str, Dict[str, Any]]] = []
                    for node_id in node_path:
                        node = mapping.get(node_id) or {}
                        if not isinstance(node, dict):
                            continue
                        message = node.get("message")
                        if not isinstance(message, dict):
                            continue
                        if _is_hidden(message, include_hidden=include_hidden):
                            continue
                        msg_create_time = message.get("create_time")
                        msg_ct_f = float(msg_create_time) if isinstance(msg_create_time, (int, float)) else None
                        tmp.append((msg_ct_f, node_id, message))
                    iterable = tmp

                for msg_ct_f, node_id, message in iterable:
                    role = None
                    author = message.get("author") or {}
                    if isinstance(author, dict):
                        role = author.get("role")
                    role = role if isinstance(role, str) else None
                    role = role or "unknown"

                    author_name = None
                    if isinstance(author, dict):
                        author_name = author.get("name")
                    author_name = author_name if isinstance(author_name, str) else None

                    msg_create_time_iso = _utc_iso_from_float(msg_ct_f)
                    content_type, text = _extract_message_text(message)
                    if not text:
                        continue

                    seq += 1
                    message_id = message.get("id")
                    message_id = message_id if isinstance(message_id, str) else None

                    cur.execute(
                        """
                        INSERT INTO messages(
                          conversation_id, seq, node_id, message_id, role, author_name,
                          create_time, create_time_iso, content_type, text
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            conversation_id,
                            seq,
                            node_id,
                            message_id,
                            role,
                            author_name,
                            msg_ct_f,
                            msg_create_time_iso,
                            content_type,
                            text,
                        ),
                    )
                    inserted_messages += 1
                    md_rows.append((role, msg_create_time_iso, text))

                if write_markdown_dir is not None and md_rows:
                    _write_markdown(write_markdown_dir, conversation_id, title, md_rows)

                processed += 1
                if progress_every > 0 and processed % progress_every == 0:
                    conn.commit()
                    print(
                        f"indexed {processed} conversations, {inserted_messages} messages...",
                        file=sys.stderr,
                    )
        if limit is not None and processed >= limit:
            break

    conn.commit()
    if reindexed_conversations > 0:
        cur.execute(
            "INSERT INTO index_meta(key, value) VALUES('indexer_version_applied', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (INDEXER_VERSION,),
        )
        cur.execute(
            "INSERT INTO index_meta(key, value) VALUES('index_mode_applied', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (requested_mode,),
        )
        cur.execute(
            "INSERT INTO index_meta(key, value) VALUES('include_hidden_applied', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("1" if include_hidden else "0",),
        )
        conn.commit()
    conn.close()
    print(
        f"done: indexed {processed} conversations, {inserted_messages} messages into {db_path}",
        file=sys.stderr,
    )


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Index a ChatGPT export conversations.json into SQLite FTS (optional per-conversation Markdown)."
    )
    parser.add_argument(
        "--input",
        default=default_export_path(),
        help="Path to an export ZIP, directory, conversations.json, or conversations-*.json glob",
    )
    parser.add_argument(
        "--db",
        default=default_db_path(),
        help="SQLite DB path (default: configured current database)",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include visually-hidden/system/internal messages in the index",
    )
    parser.add_argument(
        "--current-path-only",
        action="store_true",
        help="Index only the final conversation path (root -> current_node), not all nodes",
    )
    parser.add_argument(
        "--no-incremental",
        action="store_true",
        help="Disable incremental mode (reindex all conversations without requiring --reset)",
    )
    parser.add_argument(
        "--write-md",
        default=None,
        help="If set, write per-conversation Markdown files into this directory",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Index only the first N conversations (for quick tests)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Commit and print progress every N conversations (default: 100)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the existing DB (and -wal/-shm) before indexing",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    db_path = Path(args.db)
    write_md_dir = Path(args.write_md) if args.write_md else None

    index_export(
        input_path=input_path,
        db_path=db_path,
        include_hidden=bool(args.include_hidden),
        write_markdown_dir=write_md_dir,
        limit=args.limit,
        progress_every=args.progress_every,
        reset=bool(args.reset),
        all_nodes=not bool(args.current_path_only),
        incremental=not bool(args.reset) and not bool(args.no_incremental),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
