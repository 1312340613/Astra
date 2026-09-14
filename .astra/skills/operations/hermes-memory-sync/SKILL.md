---
name: hermes-memory-sync
description: Use when the user asks to sync, migrate, import, refresh, or verify Hermes conversation history or memory in Astra.
---

# Hermes Memory Sync

## Overview

Synchronize `~/.hermes/state.db` into Astra's `.astra/sessions.db` with the repository importer. Routine runs are message-level and idempotent: new messages are inserted, changed mapped messages are updated, and existing target history is not deleted.

## Required workflow

Run from the Astra repository root. Resolve and report the absolute source and target paths before mutation. Stop if the source is missing or both paths resolve to the same file.

1. Preview the delta:

```bash
.venv/bin/python scripts/import_hermes_history.py --dry-run
```

If the preview reports zero new and zero updated messages, report that history is current and stop.

2. Make a SQLite online backup of `.astra/sessions.db` before every real sync. Use Python's `sqlite3.Connection.backup`, choose a timestamped `.bak-hermes-sync-YYYYMMDD-HHMMSS` path, refuse to overwrite an existing backup, and verify the backup with `PRAGMA integrity_check`.

3. Run the normal incremental sync:

```bash
.venv/bin/python scripts/import_hermes_history.py
```

Do not use `--full` for routine sync. Use it only when the user requests a complete reconciliation or when repairing sync metadata.

4. Verify the target database:

- `PRAGMA integrity_check` returns `ok`.
- `messages` and `messages_fts` have equal row counts.
- Every `hermes_sync_messages.target_message_id` resolves to a `messages.id`.
- `(source_session_id, source_message_id)` mappings are unique.
- A second `--dry-run` reports zero new and zero updated messages. If Hermes received new messages during the run, repeat the normal sync once and verify again.

5. Report the backup path, new/updated session and message counts, target totals, and verification result.

## Privacy and safety

Do not print message content, message previews, or credentials. Session titles printed by the importer should not be copied into the final response. Do not delete target-only messages or modify the Hermes source database. On failure, leave the backup intact, report the exact failed check, and do not claim the sync completed.

## Common mistakes

- Copying a live WAL database with plain file copy: use SQLite online backup.
- Treating an existing session as fully synchronized: the importer tracks individual source messages.
- Checking only file existence: verify integrity, FTS parity, mappings, and a clean second dry-run.
- Running from another checkout: pass explicit `--source-db` and `--target-db` paths when the defaults are not the intended databases.
