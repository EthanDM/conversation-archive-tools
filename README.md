# conversation-archive-tools

Local-first tools for indexing, searching, and reviewing AI conversation archives.

The project indexes official ChatGPT and Claude exports and local Codex and Claude Code sessions into separate SQLite FTS5 databases. Everything stays on your machine: the tools do not upload conversations, connect to a hosted service, or write to ChatGPT memory.

## What it supports

- Official ChatGPT exports in either `conversations.json` or sharded `conversations-*.json` form.
- Claude conversation JSON, ZIP, and multipart export snapshots.
- Local Codex JSONL sessions from `~/.codex/sessions`.
- Full-text search, source-linked transcripts, topic reports, and local reflection indexes.
- Conservative Codex context-candidate reports: candidates require user-message evidence and are flagged for sensitive or operational material before review.

## Requirements

- Python 3.9+ with SQLite FTS5 support. No third-party Python dependencies are required.
- A local ChatGPT export and/or local Codex session archive.

## Install (optional)

Install the package to use the local command-line entry points:

```bash
python3 -m pip install .
conversation-archive-index-chatgpt --help
conversation-archive-index-codex --help
```

Before indexing an export, inspect it without writing data:

```bash
conversation-archive-audit --input /path/to/chatgpt-export.zip
```

## ChatGPT export quick start

Create a directory containing one official ChatGPT export, then configure and index it:

```bash
conversation-archive-configure \
  --archive-root "/path/to/ChatGPT Archive" \
  --current-export "YYYY-MM-DD"

conversation-archive-refresh-chatgpt
conversation-archive-search-chatgpt '"project planning"' --context 2
conversation-archive-show-chatgpt --title "project planning" --list
```

The indexer accepts either an extracted export directory or the downloaded ZIP directly.

`conversation-archive-refresh-chatgpt` creates the active local database at:

```text
~/Library/Application Support/Conversation Archive Tools/current.sqlite
```

You can instead pass `--input` and `--db` directly to `conversation-archive-index-chatgpt` for a one-off index. The configured paths may also be overridden with `CHATGPT_EXPORT_INPUT`, `CHATGPT_EXPORT_DB`, and `CHATGPT_EXPORT_CONFIG`.

## Claude export quick start

```bash
conversation-archive-index-claude --input /path/to/Claude-Archive/exports/full/YYYY-MM-DD
conversation-archive-search-claude '"project planning"' --role user --context 2
conversation-archive-show-claude --title "project planning" --list
conversation-archive-show-claude --conv <conversation-uuid>
```

Input may be a conversations ZIP, JSON file, or export directory. Directory discovery prefers
`extracted/conversations.json`, then `conversations.json`, then all `conversations-*.zip`
parts. Each import replaces the previous Claude snapshot transactionally. Invalid imports
leave the previous index intact; databases from other providers are refused.

The separate database defaults to
`~/Library/Application Support/Conversation Archive Tools/claude.sqlite`.
Use `CLAUDE_EXPORT_INPUT` and `CLAUDE_EXPORT_DB` for defaults; explicit `--input` and
`--db` take precedence. Input is required unless the environment supplies it.

Only user and assistant text is indexed. Structured text blocks take precedence over
legacy text fields. Thinking, tool payloads, attachment contents, account metadata,
project documents, saved memories, and generated summaries are excluded. Attachment
names appear as references, without implying that files are available. Transcripts
retain exported order and parent UUIDs and display every exported message, rather than
inferring a selected branch. `--max-chars` truncates each displayed message.
Search and transcript commands read existing databases without creating or changing them.

## Codex session quick start

Codex sessions are indexed separately from ChatGPT history:

```bash
# Initial build; reads local and available archived Codex sessions by default.
conversation-archive-index-codex --reset

# Later runs reindex only changed JSONL or compressed JSONL files.
conversation-archive-index-codex

conversation-archive-search-codex '"browser-only"' --context 2
conversation-archive-show-codex --title "personal site" --list
conversation-archive-show-codex --session <session-id> --max-chars 2000
```

Codex indexing and publishing support both `.jsonl` and Zstandard-compressed `.jsonl.zst` sessions. Compression requires the `zstandard` package installed with this project. A refresh fails without committing changes if a compressed session is corrupt or incomplete. A reset builds a replacement first and requires the old index to be free of SQLite sidecars; close other connections and allow SQLite to checkpoint before retrying. Do not run filesystem restores, copies, or synchronization that write the index or its sidecars while reset is running: those operations bypass SQLite locks and can leave the rebuilt index inconsistent. The default local input is `~/.codex/sessions`; local refresh also includes `~/.codex/archived_sessions` when available. Set `CODEX_SESSION_ARCHIVED_INPUT` to select a different archived-session directory, including with a custom local source. An explicitly configured or previously indexed archived directory must exist for refresh to proceed. When active and archived roots are paired, incremental refresh retains missing local records so sessions moving between roots remain searchable; use an explicit reset to remove records whose source files were deleted.

The Codex index retains filtered user and assistant text for search. It excludes developer/system wrappers, injected skill instructions, function calls/results, reasoning, terminal/event logs, and images. Override defaults with `CODEX_SESSION_INPUT` and `CODEX_SESSION_DB`.

### Share Codex history between Macs

Keep Codex and every SQLite database local. To search history from multiple Macs, explicitly publish each Mac's JSONL or compressed JSONL session files into a private shared archive, then build a local index from that archive. This command never uploads data itself; the archive location is a local filesystem path managed by you.

```bash
# On main-mbp: one-time claim for its populated legacy namespace
conversation-archive-publish-codex --machine-id main-mbp --claim-existing-machine-id

# On neo: one-time claim for its populated legacy namespace
conversation-archive-publish-codex --machine-id neo --claim-existing-machine-id

# On main-mbp, after both claims are visible in shared storage
conversation-archive-publish-codex --machine-id main-mbp --confirm-machine-id-sync

# On neo, after both claims are visible in shared storage
conversation-archive-publish-codex --machine-id neo --confirm-machine-id-sync

# On either Mac, after the shared storage has synchronized
conversation-archive-index-codex --shared --reset
```

By default, the shared archive is `codex-sessions/` beneath the configured ChatGPT Archive root. Override it with `--archive-root` or `CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT`; set `CONVERSATION_ARCHIVE_MACHINE_ID` to avoid passing `--machine-id` each run. Claims only create reservations; after the shared storage synchronizes, rerun with `--confirm-machine-id-sync` to copy sessions. The publisher preserves each Mac's namespace, copies only changed `.jsonl` files atomically, and never deletes source or archived files.

Each Mac also has a private local installation UUID at `~/Library/Application Support/Conversation Archive Tools/installation-id`. Before publishing, it atomically reserves its shared machine ID at `.machines/<machine-id>.json`. A reservation can only be reused by the same installation UUID. Existing populated namespaces created before this safeguard require the explicit one-time `--claim-existing-machine-id` command shown above; the publisher never silently claims them.

After Migration Assistant, a restore, or a clone, choose a new machine ID and run once with `--rotate-installation-id` before claiming it. This generates a new local ownership identity instead of reusing the copied installation ID.

Do not store `codex_sessions.sqlite` in shared storage or symlink `~/.codex/sessions`. The JSONL archive is shared; each Mac's SQLite index stays local.

## Context review

Generate a source-linked review queue from Codex sessions:

```bash
conversation-archive-context-candidates
```

Candidate claims require retained user-message evidence; assistant output is context only. The report excludes operational/work messages and flags health, relationships, finance/legal, and political/personal content for deliberate review. A possible overlap signal against the local ChatGPT index is not a claim about ChatGPT's saved Memory.

Use `--rules path/to/rules.json` to supply your own candidate wording and sensitive-category patterns; [context_rules.example.json](examples/context_rules.example.json) shows the format.

Generated reports default to:

```text
~/Library/Application Support/Conversation Archive Tools/output/
```

Set `CONVERSATION_ARCHIVE_OUTPUT` or pass `--out` to choose another private location. Generated outputs, transcript exports, and knowledge-base material are ignored by Git.

## Handoff bundles

Export selected indexed conversations for manual review or sharing. Redactions are applied before the output is written:

```bash
conversation-archive-handoff --conv <conversation-id> --out /private/path/handoff.md \
  --redact 'name@example\\.com'
```

Review the output before sharing. It preserves source conversation IDs but never transmits content.

## Other local reports

```bash
python3 -m conversation_archive.experimental.report_topic 'book AND writing' --max-conversations 20
python3 -m conversation_archive.experimental.context_pack 'book AND writing' --limit 10 --context 3 --out /private/path/context.md
python3 -m conversation_archive.experimental.book_report --out-dir /private/path/book-report
python3 -m conversation_archive.experimental.startup_ideas_report --out /private/path/startup-ideas.md
```

## Experimental analysis scripts

The `book_report.py`, `philosophy_report.py`, `startup_ideas_report.py`, `build_phase*`, and knowledge-base scripts are local analysis experiments. They remain available for users who want them, but the supported public core is export/session indexing, search, transcript display, historical indexing, context review, and the optional local dashboard.

The optional local dashboard can be built and served with:

```bash
conversation-archive-build-reflection-indexes --reset
conversation-archive-serve
```

## Development

```bash
python3 scripts/release_audit.py
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the complete local release gate. GitHub Actions runs the same privacy audit, tests, compile check, and installed-command smoke tests on supported Python versions.

## Privacy and publication boundary

This repository contains code and synthetic tests only. Do not commit exports, SQLite databases, generated reports, transcript directories, configuration containing personal paths, or credentials. Review `git status` and the tracked-file set before publishing changes.

## License

[MIT](LICENSE)

## Claude Code sessions

Claude Code's local JSONL history is separate from Claude account exports:

```bash
conversation-archive-index-claude-code
conversation-archive-search-claude-code '"project planning"' --context 2
conversation-archive-show-claude-code --title "planning" --list
conversation-archive-show-claude-code --session <session-id>
```

The input defaults to `$CLAUDE_CONFIG_DIR/projects` when `CLAUDE_CONFIG_DIR` is
set, and `~/.claude/projects` otherwise. The database defaults to
`~/Library/Application Support/Conversation Archive Tools/claude_code_sessions.sqlite`.
Override with `--input` / `--db` or `CLAUDE_CODE_SESSION_INPUT` /
`CLAUDE_CODE_SESSION_DB`. Re-run indexing to refresh changed files; use
`--no-incremental` to reparse all files. Missing source files do not erase archived
sessions. Other providers' databases are refused.

Only user and assistant text is indexed, including external channel prompts. Thinking,
tool payloads, images, internal metadata messages, and system-origin prompts are
excluded. Message UUIDs, parent UUIDs, sidechain flags, timestamps, project paths,
and custom titles are retained.
Transcripts follow file order and show branch markers; no branch is selected.
Malformed JSONL lines (including a partially written active-session tail) are
skipped. This does not collect regular Claude web/desktop chats or Cowork history.

Without an installed package, run commands from this checkout with
`PYTHONPATH=src python3 -m conversation_archive.claude_code.index` (or `.search` /
`.show`).

## Refresh before local-session search

Codex and Claude Code search commands incrementally refresh local session files
before querying, so new sessions and appended messages are searchable immediately.
Unchanged files are skipped. Search now writes to the session index by default;
`--no-refresh` searches an existing snapshot read-only and needs no source files.
Transcript display and account-export searches do not refresh.

```bash
conversation-archive-search-codex '"project planning"'
conversation-archive-search-claude-code '"project planning"' --no-refresh
conversation-archive-search-codex 'planning' --shared
conversation-archive-search-codex 'planning' --input /path/to/sessions --db /path/to/index.sqlite
```

For custom `--db` paths, supply `--input` (or Codex `--shared`) to refresh, or
`--no-refresh` to preserve the snapshot. Input and database environment defaults
remain supported. Refresh errors stop the search with exit code 2 rather than
silently returning stale results. Only the supplied local/shared source is read;
search never downloads sessions. Source pruning/retention follows each provider's
existing indexer behavior.

## Optional archive MCP

Install with Python 3.10+ in a dedicated environment:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install '.[mcp]'
.venv/bin/conversation-archive-mcp
```

The optional SDK uses its v1 API (`mcp>=1.12,<2`); the dependency-free CLI
continues to support Python 3.9. The MCP serves local stdio only, using the same
configured database paths and environment overrides as the CLI. It does not
require the dashboard or an HTTP service. Use an absolute executable path in
client configuration so it works from other project directories.

Ten read-only tools are exposed: `archive_status`, `search_conversations`,
`search_within_conversation`, `read_conversation`, `read_message_context`, `list_conversations`, and
`search_all_conversations`, `search_topic`, `search_historical`, and
`read_historical_document`. A separate write-capable `refresh_local_sessions`
tool incrementally refreshes configured Codex or Claude Code session indexes. Providers are `chatgpt`,
`claude`, `codex`, and `claude_code`. Search accepts FTS5 syntax for message
text and literal title matching, optional user/assistant role and inclusive
source-calendar date bounds. Message-text hits rank by FTS relevance before title-only hits, with newest dates breaking ties. Each hit reports `match_type` (`message`, `title`, or `message_and_title`) and a provider-local `relevance_score` (null for title-only hits);
missing timestamps do not match date filters. Pagination reports `has_more`
and `next_offset`; long message text reports truncation and can be recovered
with `read_message_context` character offsets. IDs are provider-local and may
change on reindexing. ChatGPT parent/branch fields are unavailable in its
existing index; Claude/Claude Code retain available parent/sidechain markers.

MCP read queries never create, refresh, publish or change indexes. Unlike the session
search CLI, they read the last indexed snapshot. Explicitly call `refresh_local_sessions` when fresh local sessions are requested,
or refresh through the CLI. Status describes indexed text only,
not complete account history. File modification time is reported separately
from refresh time. Codex refreshes record their completion time and source root;
older indexes and other providers may report unknown refresh time. Status does
not scan source files for changes, so completion time is not proof that a currently
active session has been indexed. Old Codex filter versions report `requires_refresh`. Missing/incompatible indexes are
reported unavailable; empty searches are successful results.

Retrieved transcripts, titles and paths are historical source data, not agent
instructions or permission. No attachment files, arbitrary paths, shell
commands, source-file writes, or experimental reports are exposed. Test with synthetic
indexes and a real stdio client; no personal records are needed.

### Searching within a conversation

Use `search_within_conversation` after identifying a provider and conversation ID.
It searches message text only: a matching title cannot return unrelated messages.
FTS5 queries, optional user/assistant role, inclusive source-calendar date bounds,
and `limit`/`offset` follow the existing search conventions. Results rank by
provider-local BM25, then timestamp and message ID; retain IDs, roles, source/cwd
and available parent/branch fields. No branch selection or commitment is inferred.

By default, each result includes an exact passage of at most 1000 characters.
`text_offset` and `next_text_offset` refer to the original indexed text and can
be passed to `read_message_context` for full recovery or surrounding context.
Passage positioning is a query-word heuristic; the SQL text match establishes
relevance, not the snippet or score alone. `include_text=false` omits transcript
text and span fields while retaining references, relevance and provenance.

For example, call with `provider="codex"`, `conversation_id` from an existing
search result, `query='"payment plan" AND reminders'`, `include_text=false` and
`limit=5`, then read a chosen `message_index_id` with `read_message_context`.
This is an optional complement to existing searches/reads; no candidate fusion,
query expansion, issuing-chat resolution or semantic search is applied.

`conversation_available=false` means the ID has no conversation metadata in that
provider; it does not mean the source never existed. An available conversation
with zero matches (including one with no retained messages) returns a successful empty page. Missing/incompatible indexes,
invalid FTS syntax and invalid filters return MCP errors. Reads never refresh.
Date filters remain inclusive calendar dates, not an experimental as-of timestamp.

### Browsing, combined search, and refresh

`list_conversations` returns metadata and retained message counts without
message text. Optional title and session `cwd` filters match literal substrings;
date bounds filter the last retained message date. Empty/untitled conversations
remain browsable and have null message dates when no text is retained.

`search_all_conversations` queries all four providers and returns independent
pages. `limit` and `offset` apply **per provider**, not globally. Each provider
reports availability and its own continuation; use `search_conversations` with
that provider's `next_offset` to continue it independently. There is no global
ranking or duplicate inference. Missing indexes do not hide other providers'
results; the response explicitly reports partial coverage.

`refresh_local_sessions` accepts only `codex` or `claude_code`, using configured
input/database paths and the existing incremental indexers. It exposes no path,
reset, shared-archive publication, or export-import arguments. It returns indexed
file, unchanged file, and retained message counts plus completion time. Source
files are never modified. Reads stay independent and never trigger refresh.

Refresh can change message IDs and replaces changed-file index rows. Codex's
indexer removes missing shared-source rows, but retains missing local rows when active and archived roots are paired; Claude Code retains missing source history. The MCP therefore marks refresh as write-capable and potentially
destructive, and refuses a Codex index with source paths outside its selected configured
inputs. For shared plus local and archived history, explicitly use
`refresh_local_sessions(provider="codex", include_shared=true)` or the CLI
`conversation-archive-index-codex --shared --include-local`. Both read the source
union without publishing local files. Default CLI refreshes retain a previously indexed configured shared root. Explicit `--input` and `--shared` select their specified scope; an incremental CLI refresh refuses to narrow an existing index. Use a separate database or an explicit index `--reset` to change scopes. Without this option, shared indexes are
refused by the local-only refresh tool. Concurrent refreshes in one MCP process are
refused. A failed refresh may have already committed some file updates; retry
finishes incremental work rather than rolling back the entire refresh.

### Topic evidence and historical coverage

`search_topic` groups the complete query match set by conversation/session and
returns exact scoped message/conversation counts, first/last matching timestamps,
and up to three bounded samples per group. It defaults to `role="user"` and
chronological order. Pass `role=null` for both roles or `order="recent"` for
newest groups. Title and FTS matching follow `search_conversations`. Pagination
is by conversation group, not message. `samples_partial` explicitly identifies
incomplete samples. Keyword coverage is not a claim that every semantic occurrence
of a topic was found; use additional queries and inspect original context.

`search_historical` searches the existing separate historical document index.
`read_historical_document` retrieves complete text in 8000-character chunks by
indexed ID. Results preserve content hash, source kind/path, format, and available
conversation ID. Historical indexing deduplicates identical document text and
retains one source path; it does not retain every duplicate snapshot provenance.
These documents lack reliable structured role/date fields. They can overlap
current conversations and are not merged into message-level counts.

Set `include_historical=true` on `search_all_conversations` to receive a separate
historical page. Its pagination is independent, and role/date filters apply only
to message providers; `role_and_date_filters_applied=false` makes that historical
limitation explicit. Status includes historical availability and source-kind
counts. No tool opens source paths, imports raw exports, or automatically rebuilds
historical indexes.

### Session search quality and migration

Session FTS tables store their title projection directly. Refresh automatically
repairs older external-content session FTS schemas transactionally, preserving
message IDs during the schema repair. This fixes title/snippet/ranked queries and
keeps replacements/deletions consistent in both Codex and Claude Code indexes.

Codex indexer version 2 excludes machine approval-review sessions identified by
`guardian_review`/guardian source metadata, plus leading legacy approval envelopes.
Ordinary human review requests and non-guardian subagent history remain searchable.
Runtime open-Page wrappers are excluded. A filter-version change reparses unchanged
source files on the next refresh so old pollution is removed; reindexing may change
message IDs. Missing sources follow the indexer's documented retention policy.
For configured shared Codex history, use `include_shared=true` on the refresh
tool to include local and archived sessions, or use the CLI's
`--shared --include-local` options. Plain `--shared` retains its original
shared-only behavior. Unavailable input roots fail before modifying the index.
Duplicate session IDs resolve to one indexed session using the existing source
mtime rules. No shared session files are published by refresh.
