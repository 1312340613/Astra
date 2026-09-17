# macOS Transactional App-State Acceptance Template

This is an unfilled template, not an executed run. Copy it into an ignored local
evidence directory before recording measurements. Keep raw run metadata,
process/window identifiers, screenshots and personal desktop observations out of
the public repository. Publish only a redacted summary of the relevant behavior
and limitations.

## Run metadata

Complete these fields in the local copy only:

- Date/time/timezone:
- Public Git revision and helper build identity:
- macOS and tested application versions:
- Accessibility/Screen Recording/CGEvent posting preflight:
- Local evidence directory:
- Deterministic gates and registry immutability checks:

## Transactional app-state matrix

| App/window | Commit/helper hash and app version; Exact app/window refs and stable OS identity | Frontmost PID/bundle and Cursor coordinates before/after | PNG validity, target bounds, and ordinary AX count | Smart detail count / truncation reasons | Artifact cleanup result | Result |
| --- | --- | --- | --- | --- | --- | --- |
| Fixture | | | | | | NOT RUN |
| TextEdit | | | | | | NOT RUN |
| VS Code/Electron | | | | | | NOT RUN |
| WPS Home | | | | | | NOT RUN |
| WPS document window | | | | | | NOT RUN |

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

## Reporting scope

Record only fresh `computer_apps -> computer_get_app_state(text_detail=on)`
transactions with `computer_close` cleanup. Keep fixture results separate from
real application compatibility. An Electron fixture does not establish VS Code
compatibility, and a prior run does not establish a result for the current build.
Link the local report to its local matrix; do not commit the populated matrix.
