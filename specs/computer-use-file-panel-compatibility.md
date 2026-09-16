# Native file-panel compatibility

The 2026-09-16 investigation found a visible Edge file panel whose AX root was an
`AXSheet` with a description but no title. The window inventory already contained
its exact geometry. Title-only matching rejected it, reported `target_gone`, and
post-action recovery suggested the obscured parent. The user approved improving
this behavior after reviewing the investigation and proposed repair.

## Decision

Use one bounded accessibility window-name reader throughout catalog matching,
binding and foreground focus validation. A complete nonempty AXTitle stays
authoritative. Only an AXSheet with a complete missing/empty title may use its
complete nonempty AXDescription. Failed or truncated reads never gain a fallback.
The rule is structural, without localized titles or browser-name allowlists.
PID, exact geometry, unique candidates, retained AX identity and live focus remain
mandatory. Ordinary unnamed windows and ambiguous candidates remain rejected.

Real acceptance exposed another level: Go to Folder is an unnamed AXSheet whose
parent is the Open AXSheet. Recover at most eight direct sheet links from a unique
owned AXWindow along the current focus ancestry. Each link must retain its owner,
sheet role and containment; groups, detached/cyclic chains and ambiguous parents
do not qualify. An unnamed proven sheet may pass foreground mapping only when a
single same-PID, same-bounds, explicitly unnamed CG window exists. Exact retained
AX identity and CG window ID must still match the approved target. Failed name
reads and ordinary unnamed windows remain rejected.

Acceptance also found two downstream issues. Plain Unicode events inherited the
modifier flags of Command-A, so the field remained unchanged despite input
acknowledgement. Explicitly clear modifiers on plain Unicode down/up events;
explicit shortcut events retain their requested modifiers. In long file-column
trees, read immediate sheet buttons before descending into directory branches so
Open/Cancel remain observable within the existing AX budget. This reorders reads
only, without adding controls, authority or a larger traversal budget.

Installed-bundle acceptance found that offscreen ancestor directory columns can
also consume the node budget before the current filename. Within a sheet, read
buttons first, then children on its proven focus ancestry, then other children.
Resolve that same-process, acyclic path once with a 32-link bound, and reorder only
children already returned by AXChildren. Missing ancestry grants no priority and
does not add descendants or input authority. Keep the existing observation budget.

ScreenCaptureKit can return the entire parent composite scaled into a sheet's
requested image size. For a proven sheet, resolve its directly owned root window,
capture that exact root at its own geometry, then crop to the selected sheet's
global bounds before publishing the image. Revalidate the root ID and geometry
after capture, as well as the existing selected-target checks. Ambiguous ancestry,
changed geometry and nonintegral crops fail closed. No full-parent image is
published, and screenshot pixels now share the selected sheet's coordinate space.

Changing error text alone would not restore interaction. Accepting all unnamed
windows by geometry would weaken disambiguation. The scoped name reader addresses
the observed failure while retaining the existing checks.

When a catalog record still exists but no unique AX window maps to it, return
`ax_window_unmatched` instead of `target_gone`. Expose a fixed safe explanation and
state-dependent recovery; never leak native titles or paths in failure messages.
Actually missing or replaced targets retain their existing errors.

Post-action recovery must not recommend the previous window if an unbindable
window overlaps it (or the geometry needed to exclude that possibility is
unavailable). Report the unmatched blocker and wait for a changed observation.
An unrelated nonoverlapping window must not prevent recovery. New uniquely
bindable windows can still be suggested. No recovery grants action authority or
replays an uncertain input batch.

## Validation and delivery

- [x] Inspect the saved session, native diagnostics, current implementation and
  installed helper identity.
- [x] Present the minimal repair and alternatives; receive user authorization.
- [x] Record the bounded design and check scope, failure semantics and acceptance.
- [x] Add and run regression coverage for description-only sheets, ordinary
  windows, failed/truncated reads, duplicate identities, unmatched-vs-gone errors,
  and blocking/nonblocking transition recovery.
- [x] Run the native suite and relevant Python protocol/backend/tool tests.
- [x] Exercise a real local file input with a temporary non-sensitive file through
  the production CU tools; verify selection from the resulting page state.
- [x] Compile the release helper, integrate into local main, and report the exact
  installed/running build plus any acceptance or permission boundary.

A candidate bundle is built separately before any intentional installation; do
not replace an active helper merely as a side effect of committing. Automated
acceptance uses local fixtures without sending files to an upload service.

Real Edge acceptance covers both AX element clicks and image-coordinate clicks:
open the local file panel, enter a path in its nested Go to Folder sheet, read back
the exact text, select the file, and verify the selected filename in the local
page. Both cases passed and closed their own fixture windows. No uncertain action
was replayed.

Validation on 2026-09-16: the serial native suite passed 743 tests; the relevant
Python computer/macOS suite passed 1171 tests with one skip. The two live Edge
cases passed. Ruff, Pyright for the changed runtime modules, the skill contract
tests and `git diff --check` passed. Recorder test events explicitly clear their
synthetic modifier flags so live desktop shortcuts cannot alter fixture meaning.

Installed-bundle acceptance also passed both real file-selection cases, with
Accessibility and Screen Recording available. A follow-up on a live Canvas
upload form verified the selected attachment through the production CU tools,
the fresh Accessibility tree and the screenshot. The final Submit action was
not executed; server upload and submission are outside this acceptance result.
Restart Astra after updating to load the new CU implementation.
