# macOS Transactional App-State Real-Machine Acceptance Matrix

## Run metadata

- Date/time/timezone: 2026-08-31 19:50 +08 (Asia/Singapore)
- Git/helper: `ec02fa00c7a7df43fbc8255b14ddb8c1915e8c61`; built exactly once; helper SHA-256 `8555193ce1fdf194396fc8836b3831eada12209c9324877c327eadcfd10058e0`.
- macOS: 26.5.2 (25F84). Accessibility/Screen Recording/CGEvent posting: true.
- Evidence root (0700): `/tmp/astra-computer-acceptance-20260831-195040`.
- Gates: schema-2 empty registry assertion; Swift 369 + helper harness 62; Python `661 passed, 1 skipped`; build/codesign/resource/symbol gates all passed. Registry SHA stayed `4b85375a77e37a7d220ab2c374d33610baa5d0a4d42b5688f2d524a5db907959`.

## Transactional app-state matrix

| App/window | Commit/helper hash and app version; Exact app/window refs and stable OS identity | Frontmost PID/bundle and Cursor coordinates before/after | PNG validity, target bounds, and ordinary AX count | Smart detail count / truncation reasons | Artifact cleanup result | Result |
| --- | --- | --- | --- | --- | --- | --- |
| Fixture | repair root `cleanup-repair-1788179000`; `app_485e1f39-8d30-404c-8fbf-dbe099f7b59a` / `win_1f07a226-6019-4f85-b5a8-638ed2eb451e`; test-owned `dev.astra.computer-fixture` 1.0.0 | Codex PID 47151; (152.03125, 610.71875), unchanged | absolute PNG path/name recorded; 103242 B SHA `a54d17d4...`, 0600; 1434x1424; 110 AX | absolute detail path/name recorded; 4706 B SHA `76ea0f5b...`, 0600; 29 nodes/depth 3, untruncated | `computer_close`; PNG/detail `lstat=FileNotFoundError`, exists=false; cache only `.cleanup.lock`; PID 86576 nonexistent | PASS |
| TextEdit | repair root `cleanup-repair-1788179000`; `app_9a3b9b4a-4e13-4e30-83c9-12f6cf27e84f` / `win_4d75b628-972c-4b6c-a792-1e8b02c45c07`; exact test `document_path`, TextEdit 1.20 | Codex PID 47151; (152.03125, 610.71875), unchanged | absolute PNG path/name recorded; 39526 B SHA `c31bde96...`, 0600; 1312x844; 278 AX | absolute detail path/name recorded; 2390 B SHA `a4349dd0...`, 0600; 13 nodes/depth 3, untruncated | `computer_close`; pair lstat absent/exists=false; cache only lock; PID 86786 nonexistent | PASS |
| VS Code/Electron | **Electron 40.10.2 fixture only, not VS Code compatibility.** repair root `cleanup-repair-1788179000`; `app_436e915e-2067-42dc-b17d-1a579ac5b959` / `win_dddbcd4e-7a58-4785-8e24-82cf3d86b695`; exact unique title | Codex PID 47151; (152.03125, 610.71875), unchanged | absolute PNG path/name recorded; 58106 B SHA `670186a9...`, 0600; 1600x1200; 200 AX | absolute detail path/name recorded; 2480 B SHA `2bb97290...`, 0600; 13 nodes/depth 6, `reported_ax_subtree`, untruncated | `computer_close`; pair lstat absent/exists=false; cache only lock; PID 86946 nonexistent; run-scoped userData created 0700 then absent after cleanup | PASS — Electron fixture only; does not establish VS Code compatibility |
| WPS Home | Only user PPTX and an untitled unprovable WPS window existed; neither selected | N/A | N/A | N/A | unchanged | NOT RUN -- test-owned Home isolation unavailable |
| WPS document window | No test-owned WPS document created; user PPTX untouched | N/A | N/A | N/A | unchanged | NOT RUN -- test-owned document isolation unavailable |

## Result rules

- `PASS` requires the complete row evidence, including verified PNG/bounds,
  unchanged frontmost PID and cursor for this read, and proven artifact cleanup.
- `WARN` records a completed transaction with a material coverage limitation.
  If the WPS self-drawn document body is absent from AX, record `WARN`; this is
  not an Appshot-equivalent PASS. Ordinary or Smart AX counts do not establish
  document-text completeness.
- `FAIL` records a failed transaction or a violated no-focus/no-pointer
  postcondition. `NOT RUN` records an unexecuted row without substituting
  previous evidence.
- On `unsafe_artifact` or another authority invalidation, call
  `computer_close`, start a fresh session, then get a new catalog with
  `computer_apps` and repeat `computer_get_app_state`. Do not reuse refs,
  artifacts, approvals, or partial output.

## Scope and conclusion

Every executed row was a fresh `computer_apps -> computer_get_app_state(text_detail=on)` session with `computer_close`; no focus/input/click/scroll/pointer movement occurred. No compatibility registry cell, AXValue setter, OCR, adapter, focus fallback, or synthetic-input authority was added. WPS `WARN` remains a future outcome only: it would mean incomplete AX coverage, and Appshot enrichment remains separate work.
