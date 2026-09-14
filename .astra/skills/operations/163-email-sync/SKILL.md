---
name: 163-email-sync
description: Use when the user asks to synchronize, list, search, read, or download attachments from a configured 163 mailbox.
---

# 163 Email Sync

Use the repository `checkmail` entry point. For "recent N", run `./scripts/checkmail.sh recent --limit N --json` on POSIX or `.\scripts\checkmail.bat recent --limit N --json` on Windows. Every mail query must also state both fallback outcomes: if refresh fails and cached results exist, return them labelled potentially incomplete with the last successful cache time; if no cache exists, report the failure. Use `--offline` only when the user explicitly asks or when continuing after an already-reported refresh failure.

Never ask the user to paste an authorization code into chat. If configuration is missing, tell them to set `ASTRA_163_EMAIL` and `ASTRA_163_AUTH_CODE` in the repository `.env`.

Treat the server as read-only. Do not mark mail read, move, delete, send, or download an attachment unless the user explicitly requests that attachment.

Report sync status. Preserve and report `remote_removed` and `body_truncated` states. Answer in the user's language.

Run `./scripts/checkmail.sh <command> --json` on POSIX and `.\scripts\checkmail.bat <command> --json` on Windows. Use `sync`, `recent`, `search`, `read`, `folders`, and `status` according to the user's request. Explicit attachment download is POSIX-only: run `attachment --folder FOLDER --uidvalidity N --uid N --part PART` only for one user-selected attachment; on Windows report that attachment download fails closed and do not attempt another path.
