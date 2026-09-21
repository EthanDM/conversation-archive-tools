# conversation-archive-tools

Local-first tools for indexing, searching, and reviewing AI conversation archives.

The project indexes official ChatGPT exports and local Codex sessions into separate SQLite FTS5 databases. Everything stays on your machine: the tools do not upload conversations, connect to a hosted service, or write to ChatGPT memory.

## What it supports

- Official ChatGPT exports in either `conversations.json` or sharded `conversations-*.json` form.
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

## Codex session quick start

Codex sessions are indexed separately from ChatGPT history:

```bash
# Initial build; reads ~/.codex/sessions by default.
conversation-archive-index-codex --reset

# Later runs reindex only changed JSONL files.
conversation-archive-index-codex

conversation-archive-search-codex '"browser-only"' --context 2
conversation-archive-show-codex --title "personal site" --list
conversation-archive-show-codex --session <session-id> --max-chars 2000
```

The Codex index retains filtered user and assistant text for search. It excludes developer/system wrappers, injected skill instructions, function calls/results, reasoning, terminal/event logs, and images. Override defaults with `CODEX_SESSION_INPUT` and `CODEX_SESSION_DB`.

### Share Codex history between Macs

Keep Codex and every SQLite database local. To search history from multiple Macs, explicitly publish each Mac's JSONL session files into a private shared archive, then build a local index from that archive. This command never uploads data itself; the archive location is a local filesystem path managed by you.

```bash
# One-time claim for a populated legacy namespace on the first Mac
conversation-archive-publish-codex --machine-id main-mbp --claim-existing-machine-id

# On the other Mac
conversation-archive-publish-codex --machine-id neo

# On either Mac, after the shared storage has synchronized
conversation-archive-index-codex --shared --reset
```

By default, the shared archive is `codex-sessions/` beneath the configured ChatGPT Archive root. Override it with `--archive-root` or `CONVERSATION_ARCHIVE_SHARED_CODEX_ROOT`; set `CONVERSATION_ARCHIVE_MACHINE_ID` to avoid passing `--machine-id` each run. The publisher preserves each Mac's namespace, copies only changed `.jsonl` files atomically, and never deletes source or archived files.

Each Mac also has a private local installation UUID at `~/Library/Application Support/Conversation Archive Tools/installation-id`. Before publishing, it atomically reserves its shared machine ID at `.machines/<machine-id>.json`. A reservation can only be reused by the same installation UUID. Existing populated namespaces created before this safeguard require the explicit one-time `--claim-existing-machine-id` command shown above; the publisher never silently claims them.

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
