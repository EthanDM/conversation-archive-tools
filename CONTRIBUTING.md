# Contributing

## Scope

`conversation-archive-tools` is a local-first CLI for reviewing ChatGPT exports and Codex sessions. Keep changes small, explicit, and dependency-free unless a dependency clearly improves the supported core.

The supported core is indexing, search, transcript display, context review, and the optional local dashboard. Files under `src/conversation_archive/experimental/` are useful local analysis scripts, not a stable product surface.

## Setup

Use Python 3.9 or newer with SQLite FTS5 support:

```bash
python3 -m pip install -e .
```

No account, API key, environment file, or network service is required.

## Verification

Run this gate before publishing a change:

```bash
python3 scripts/release_audit.py
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
python3 -m pip install .
conversation-archive-index-chatgpt --help
conversation-archive-index-codex --help
git diff --check
```

## Privacy boundary

Commit source code, synthetic fixtures, and generic examples only. Never commit conversation exports, Codex session data, SQLite databases, generated reports, transcript exports, configuration with private paths, or credentials.

The release audit checks tracked files and unignored additions for common runtime files, credentials, email addresses, and machine-specific home paths. It is a guardrail, not a substitute for reviewing `git status` and the actual diff.

## Change guidelines

- Preserve the separation between ChatGPT and Codex indexes.
- Keep all storage and serving local by default; do not add automatic transmission or memory synchronization.
- Update tests with behavior changes, and update `README.md` when setup, commands, privacy behavior, or supported scope changes.
- Prefer conventional commits where practical, such as `feat(index): ...`, `fix(audit): ...`, or `chore(ci): ...`.
