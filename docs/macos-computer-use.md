# macOS Computer Use

Computer Use is a local-only macOS integration for controlling one selected application window at a time. It is currently implemented for macOS 14 and later. Respect the requested channel. Native CU can operate browser forms through AX; browser-specific integration is another supported channel when the user has not selected native CU.

## Build and readiness

Build and sign the production helper from the repository root:

```bash
bash scripts/build_macos_computer_helper.sh
```

The default helper is `.astra/bin/AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper`. The helper and its containing directories must be owned by the current user and must not be symlink substitutions.

For candidate builds, pass `--output-root /absolute/private/directory` to the build
script and set `ASTRA_COMPUTER_HELPER_PATH` to the resulting executable. Use the
canonical path (on macOS, `/private/tmp`, not its `/tmp` symlink). Signed-helper
integration tests build once into private pytest storage and verify that the
repository's deployed bundle stays unchanged. A test run must not replace a live
helper as a side effect.

Computer Use requires both Accessibility and Screen Recording permission for the signed helper. `/computer status` is a cached, nonactivating UI command: it reports the last known capability/permission state, active target, and handoff state without starting the helper, prompting, or opening System Settings. The model-facing `computer_status` tool explicitly queries the helper and refreshes that cache, but it also never prompts or opens System Settings. `/computer setup` is the only command that opens privacy panes, and only for permissions reported missing. `/computer stop` closes the session and removes its request-local captures and approvals.

The TUI shows an active Computer Use indicator while a session exists. It shows the selected application when that bounded label is available and distinguishes active automation from user handoff.

## Model-facing operation

The runtime exposes nine local tools:

1. `computer_status` reports bounded readiness and session state.
2. `computer_apps` lists eligible running GUI applications and windows using opaque references.
3. `computer_get_app_state` binds one exact current-catalog application/window pair and returns its first verified observation without activating it.
4. `computer_focus` explicitly locks the session to one application/window pair for advanced/actionable work.
5. `computer_snapshot` refreshes only that bound target and returns a fresh image plus a bounded Accessibility tree.
6. `computer_act` applies one guarded action batch to the latest snapshot.
7. `computer_handoff` blocks automation input so the user can complete a protected step.
8. `computer_resume` revalidates the target and returns a fresh observation after handoff.
9. `computer_close` ends the session and clears captures, references, and grants.

### Transactional app-state read

The normal read flow is `computer_apps` followed by `computer_get_app_state`, using exact app/window refs from the latest catalog. This read does not focus the app or move the real pointer (the no-focus/no-pointer guarantee). `computer_focus` remains an explicit advanced operation, not a prerequisite for ordinary observation; after an explicit focus, `computer_snapshot` refreshes the bound target.

Choose only `bindable=true` catalog windows. On `ax_window_unmatched`, refresh the catalog and obtain new refs; never reuse or implicitly rebind an old ref. A new ref remains bound to its own exact current catalog target.

The returned PNG, bounded ordinary AX tree, and optional Smart detail are one verified transaction. Target or action invalidation clears snapshot, element, plan, and approval authority; Smart approval for `text_detail=on` is once-only and bound to the exact current catalog target, scope, mode, and focus generation. Errors are bounded and fail closed: do not infer a current ref, preserve an old artifact, or turn a partial observation into success. On `unsafe_artifact`, call `computer_close`, start a fresh session, then run `computer_apps` and `computer_get_app_state`; do not reuse any old ref or recovery result.

The ordinary AX projection counts depth by parent-to-child element edges. Its
default limit is 19 edges, covering all 20 levels of the native ordinary tree;
the `children` list and element attributes do not use extra element levels.
Web content branches now extend by up to 48 levels, bounded by 64 total levels; `form_controls` exposes deep controls independently of the ordinary projection. Typed AX trees have separate limits of 2,000 elements and
64,000 total values, so toolbar attributes do not exhaust the element allowance.
Generic metadata retains its 2,000-value and 12-edge limits; the 512-character
string, 128-field mapping and 512 KiB final JSON limits remain in force. Anchored
subtrees count from their new root with a 24-edge limit, and role-filtered trees
allow 64 edges while retaining the element and byte caps. Values with a secure
role or subrole are redacted independently of depth. If native/detail data exists
but the ordinary tree omits it, inspect projection truncation before diagnosing
an application readiness delay.

The deterministic contract tests establish these tool semantics. They are not a Task 10 live result: a real-machine matrix must independently record this machine's current helper, application, AX, PNG, cursor, and cleanup evidence.

Snapshots default to `target_window`. A `display` snapshot requires its own exact once-only approval, captures only the single bounded display that fully contains the selected target window through ScreenCaptureKit, and then expires; the next default snapshot is `target_window` again. Ambiguous multi-display, unknown-display, and oversized captures fail closed. Display snapshots never authorize input and still expose Accessibility data only for the selected application/window. Region and persistent global capture are unsupported. The native helper checks the selected PID, window ID, bounds, frontmost application, and focused Accessibility identity before and after capture. Coordinates are target-window-local and are rejected if stale or outside the recorded `capture_bounds`. Same-application modal sheets remain bound to the original target and are accepted only when their native window and Accessibility geometry are strictly contained by it. The snapshot records whether a contained overlay or selected window was used and applies that identical choice before every action; disappearance, replacement, or movement makes the snapshot stale. A separately enumerated same-application dialog keeps the normal bounded tree depth; the shallow overlay limit applies only when its focused Accessibility root differs from the selected native window.

### Keyboard failure evidence

Failed keyboard outcomes can include optional `input_diagnostics`: `stage`
(`before_key_down`, `key_down`, `before_key_up`, `key_up`, or `after_key_up`), the original
bounded error `cause`, and boolean `input_may_have_started` and `cleanup_failed`.
These fields describe execution, not the application's resulting state. A false
`cleanup_failed` flag does not prove the application processed the release. For
text input, `input_may_have_started` includes earlier characters in that action.
Old outcomes without diagnostics remain valid; updated native and Python
components should be delivered together.

If an earlier action in a batch may have changed the application, the batch and
failed action both report `unknown_outcome`; keyboard diagnostics preserve the
underlying cause. Do not infer a prohibited key from this error or replay the
batch blindly. Inspect fresh state before choosing a recovery action.

Matching key-up completes the already authorized held input through its exact
token and activity generation before post-release checks. A positively observed
ordinary focus change after a keypress within the still-exact window produces
an acknowledged outcome with `observation_required: true`. If actions remain,
the batch stops with `observation_required` and only its successful prefix;
there is no failed or delivered outcome for the suffix. If the checkpoint is
the final action, the batch succeeds with that marker. The public tool captures
fresh state under the existing target binding and includes `next_action_index`
(zero-based, equal to the batch length when no actions remain). It assesses
effects only for the sent prefix and never resumes the suffix automatically.

Window/root changes, secure or indeterminate focus, user activity, posting
failures, and deadlines retain conservative stop semantics. Multi-character
typing and explicit text targets retain strict focus matching. This is a live
observation boundary, not an application event-processing acknowledgement or a
guarantee that a delayed transition has settled. Each subsequent input still
requires its existing checks. Deliver the updated native helper and Python
runtime together; older runtimes do not understand checkpoint receipts.

Background observation distinguishes ordinary windows behind the selected
target from overlays. A separately mapped sibling must be `AXWindow` /
`AXStandardWindow` with proven visual ordering. An independently mapped
`AXWindow` / `AXDialog` also qualifies when its modal flag is explicitly false
and native ordering proves it is behind the selected window. This observation
exception does not change input or focus authority. When sibling titles cannot
identify individual native windows, an equal-geometry group can instead prove
non-occlusion: complete AX, ScreenCaptureKit and CG inventories must agree in
count and geometry, native IDs and AX objects must be unique, every AX member
must be an ordinary nonmodal window, and known mappings must be distinct. Every
remaining native candidate must be behind the uniquely bound selected target.
This proof never assigns IDs or input authority to unresolved siblings. It is
bound to the process, selected window/AX object and sibling AX objects and is
rebuilt at each observation revalidation. Unknown ordering, incomplete groups,
dialogs within unresolved groups and possible obscuration remain rejected.
Mapped dialogs with true/unknown modality or uncertain/front ordering also
remain rejected. Existing local diagnostics
include fixed `observation_validation` stage labels for identity failures.
The foreground overlay detector also uses native ordering: a contained window
proven behind the exact selected target does not switch the focus-root
preference or shorten its observation depth. Uncertain or foreground overlays
retain the existing checks.

Key names accept `ArrowLeft`, `ArrowRight`, `ArrowUp`, `ArrowDown`, `Esc`, and
`Enter` as aliases for `left`, `right`, `up`, `down`, `escape`, and `return`.
Aliases retain the same compatibility and approval checks. Pass modifiers
separately; unsupported key names are rejected before dispatch.

### Smart Snapshot text detail

`computer_snapshot` also accepts `text_detail=off|on`. The default is `off`: it remains byte-for-byte compatible with the existing request and response variant and creates no `.ax.json` file. `on` adds a verified file-only AX detail artifact, bounded metadata, and a compact inline `text_summary`; the complete detail tree is never inlined. `text_detail_path`, `text_detail_metadata`, and `text_summary` are request-local, as are the ordinary image path and inline AX observation. The existing bounded `ax_tree` remains a separate inline observation and is not replaced or enlarged by Smart Snapshot.

The artifact records only the subtree that the target application reports through Accessibility children. Its metadata therefore says `coverage=reported_ax_subtree`. It may contain reported offscreen nodes, but it does not guarantee a complete document, virtualized or lazily created nodes, application-omitted content, or content available only after scrolling. `truncated=false` means only that Astra did not hit its own limits. Snapshotting does not scroll, change focus, or mutate the target to discover more text.

The frozen limits are depth 20, 4,000 nodes, 4 KiB per structural string, 256 KiB per value, 4 MiB aggregate text, 8 MiB final AX JSON, and 5 seconds of traversal time. The compact summary is separately limited to 200 entries and 32 KiB. Each session may retain at most eight detail artifacts and 64 MiB of cumulative detail data. A bounded partial tree reports `truncated=true` with ordered exact reasons; permission, target identity, redaction, serialization, publication, or verification failure fails the Smart Snapshot as a whole.

Native code removes value, selected text, ranges, and attributed text before serialization whenever role or subrole is secure, missing, unreadable, truncated, or otherwise indeterminate. Literal canonical `AXUnknown` in either role or subrole is indeterminate. An incomplete role is emitted as `AXUnknown`, an incomplete subrole is omitted, and both cases set `redacted=true` without running a sensitive accessor. Before every actual AX accessor, traversal applies the positive remaining-time messaging timeout to that exact element and restores its inherited timeout afterward; inability to establish or restore that boundary fails the Smart Snapshot closed. Python strictly validates the schema and repeats the redaction checks before exposing the path, but it is a defensive second gate rather than the source of redaction. Detail nodes never receive actionable `element_ref` values.

Every `text_detail=on` request needs a separate once-only Smart approval bound to the current session, exact application/window target, snapshot scope, detail mode, and focus generation. A combined `scope=display,text_detail=on` request uses one exact combined display-and-text approval. The grant is consumed synchronously before capture is awaited; target, session, or focus invalidation clears it. This observation approval grants no pointer or keyboard delivery and never changes the compatibility registry.

The bounded Accessibility observation may also include the selected application's own menu bar. Menu elements are accepted only when both the application element and menu bar report the locked target PID; they remain snapshot-local and do not expose another application's or the system's UI. Automated text insertion uses only a complete, non-secure focused element's settable selected-text attribute. When AX selected-text is unsupported, it fails closed without synthetic keyboard input. It never replaces an entire value, reads a secure value, or uses the clipboard.

Each action consumes one snapshot. Action batches are never replayed or retried after input might have started. A fresh snapshot is required after an unknown outcome, target change, handoff, or failed refresh.

`verified` is narrowly typed: it covers numeric AXIncrement/AXDecrement direction, a directional owning-scrollbar value change after the scrollbar-button fallback below, or a checkbox/radio/disclosure AXPress transition observed on the retained exact Accessibility element. It does not prove broader task completion. `noop` is not completion; it means the comparable declared state stayed unchanged through the bounded settle window, so inspect a fresh snapshot and choose a different explicit action. `unknown_outcome` forbids replay. Return/default-button recovery is a new explicit action after fresh observation, never automatic.

For a coordinate-free vertical `scroll`, direct AXIncrement/AXDecrement remains the first route. If the retained target subtree instead contains exactly one directional `AXIncrementPage`/`AXDecrementPage` button with AXPress, Astra performs one AXPress; only when no valid page candidate exists may one matching arrow button be used. Finder on macOS 26.4 omits `AXEnabled` for these scrollbar child buttons, so a missing value is treated as unknown and accepted while an explicit `false` is rejected. The owning scrollbar and child button are both re-resolved and identity-checked before mutation, ambiguity fails closed, delta magnitude never causes repeats, and an uncertain AXPress is never replayed through CGEvent. This path is verified for Finder-style scrollbars. It does not solve Safari/WebKit scrolling; PID-targeted CGEvent wheel delivery remains separately compatibility-gated and Astra never silently escalates it to global HID input.

Fresh-snapshot scroll observation distinguishes why the AX route could not be verified: `scrollbar_not_found` means the requested target subtree contains no `AXScrollBar`; `scrollbar_control_not_found` means a scrollbar exists but has no eligible directional Page or Arrow control; `scrollbar_identity_not_unique` is reserved for multiple candidates or ambiguous before/after scrollbar identity. All three remain fail-closed unknown outcomes and none claim that the page moved.

Background observation and AX-native actions are the default. A real pointer action is never a silent fallback: the runtime first plans one bounded foreground fragment, asks for approval tied to that exact snapshot and action list, activates and verifies the exact PID and AX window, executes at most that fragment, and restores the previously frontmost application. User pointer or keyboard activity before dispatch cancels the fragment; activity during dispatch stops at the next action boundary and releases held input. The overlay cursor shown while planning is virtual and does not move the macOS pointer. Approved PID click, double-click, scroll, and drag must leave the real cursor at its recorded starting coordinate.

The input guard separately proves that the focused AX window maps to the exact
native target window. Stacked windows may share a PID and bounds; complete,
nonempty AX/native titles must then identify exactly one matching candidate.
Missing or colliding titles still reject. The mapped native ID, bounds and AX
identity must equal the approved target at each input boundary. A frontmost PID
alone is insufficient, and switching to a same-position sibling stops the
remaining input.

Foreground pointer compatibility is default-deny per exact `bundle_id + CFBundleShortVersionString + action`. An action is enabled only from a current real-machine record proving cursor restoration, frontmost PID before/during/after, exact receiving window, a fresh target effect, no target-external effect, and held-input cleanup. Evidence for one application, version, or action never enables another, and version ranges and wildcards are not accepted. If a cell is absent or any field fails, background AX support remains available while the pointer request returns a degraded `foreground_takeover_required`, `background_action_unsupported`, `target_not_frontmost`, `stale_snapshot`, or `unknown_outcome` response as appropriate. Astra never escalates a failed cell to global HID input.

## Approvals and protected UI

Ordinary actions may be approved for the current session/application/window. High-impact actions such as Save, Export, Return/Enter, Finder copy/rename/trash, and Terminal input use a once-only approval bound to the exact action batch and snapshot. This exact Computer Use approval is authoritative in locked, safe, permissive, and YOLO modes: a generic tool-policy approval cannot replace it, suppress its prompt, or broaden its app/window/path/effect boundary. A denial causes zero helper action calls. When a trusted local UI exposes a confirmed file path, the approval names the canonical exact path; it does not broaden to a directory or a later batch.

Secure or incomplete Accessibility identities fail closed. Secure field values are never read into snapshots, events, approvals, or logs. Authentication, payment, and other protected targets require `computer_handoff`; while handoff is active, application enumeration, focus changes, snapshots, and actions remain blocked until `computer_resume` revalidates the target.

Secure fields, IME/composition candidates, system or administrator authorization UI, permission prompts, and any target whose exact focused-window identity cannot be proved are unsupported for automated foreground input. Hand these cases to the user; do not click through them or infer success from another application.

Only bounded handoff state is durable in the UI event stream: whether Computer Use is active, whether it is handed off, permission state, and an optional application label. Window titles, Accessibility trees, helper diagnostics, screenshot paths, and image bytes are not included in replayable handoff events. Ordinary typed arguments are retained in tool-call history and events as described below.

## Request-local data

Screenshots and Accessibility observations are request-local tool results. Capture files are created with mode `0600` in a mode-`0700` session directory and verified before publication. Every session holds its directory and lease descriptors, with the advisory directory `flock` shared while the session is live and exclusive before cleanup. Startup serializes cleanup with an owned mode-`0600` root lock, preserves any peer whose lease is live, and removes a dead leased orphan (including after `SIGKILL`) only after directory/file owner, type, mode, link count, and descriptor identity checks. A legacy directory without a lease is eligible for removal only after 24 hours and only when its complete inventory consists of exact `snapshot-<32-lowercase-hex>.png` legacy artifacts; recent directories and any detail, temporary, quarantine, or unknown entry are preserved and block automatic cleanup. Symlinks, nonregular entries, wrong ownership, or identity races fail closed. They are not remote API inputs, durable conversation history, or reusable artifacts. The runtime never uses clipboard paste. Approval descriptions summarize input by character count. Ordinary typed text is retained exactly in tool-call history, events, task records and local call diagnostics, so subsequent requests and restored sessions can use the original input. Recognizable credential assignments (including JSON fields), authorization headers, URL credentials and private-key blocks are replaced with an idempotent `[redacted N chars]` marker in those durable argument records. Detection is syntax-based and cannot identify arbitrary unlabelled passwords. Original validated text still reaches the execution path. Complete markers, including legacy markers, are rejected before planning or input. The legacy `raw-tool-calls.jsonl` archive now follows the same argument persistence policy and records that policy in each new entry; historical archives are not rewritten.

With text detail enabled, `snapshot-<token>.png` and `snapshot-<same-token>.ax.json` use the same lowercase 32-hex token and the same held session-directory authority. Both temporary files are completed and file-synced before publication; success is exposed only after the PNG and JSON are renamed and the directory is synced. Python then checks owner, regular-file type, mode, link count, inode identity, size, SHA-256, strict JSON schema, and snapshot binding before returning the detail path.

The session directory and its advisory `flock` lease remain the cleanup authority. Under the approved threat boundary, cooperating Astra components obey the lease and local processes with the same UID are trusted; protection against a malicious same-UID peer that ignores the lock is out of scope. Normal invalidation preflights the complete saved PNG/JSON pair before the first unlink. A missing, replaced, relinked, permission-changed, size-changed, or otherwise mismatched member at that preflight causes zero unlink and poisons the session. After successful pair preflight, cleanup revalidates and unlinks the names sequentially; a later I/O or revalidation failure may leave one member and must poison/block reuse until terminal cleanup or explicit recovery. Temporary or quarantine names, unknown entries, and any residual whose ownership or identity is uncertain are preserved, block reuse, and require a separately reviewed manual recovery workflow. A failure during the second rename, directory sync, rollback, or quarantine handling likewise poisons publication until held-directory cleanup proves the state safe.

Before reading a selected application's snapshot tree, the helper negotiates
enhanced accessibility only when the app reports `AXEnhancedUserInterface` as
false and settable. This allows providers such as Chromium to expose web
content that is otherwise absent from the native tree. The helper makes one
request per retained application instance and reads the property back even if
the setter returns an error. Confirmed activation receives a 2.25-second settle
within the existing observation deadline. Already-enabled or unsupported apps
do not receive repeated writes. The shared app mode is not disabled on helper
close; no focus change, keyboard input or system-wide VoiceOver toggle occurs.
This is provider preparation, not evidence that every page element is present.

## Appshot source binding

Appshot binds the frontmost application's focused window using process identity
and exact geometry. When several native windows have those same bounds, it also
requires nonempty AX/native titles that identify exactly one candidate using the
existing exact or application-suffix comparison. Missing or ambiguous title
evidence still rejects the capture. The selected native ID, process, geometry
and retained AX root are revalidated; a title used to disambiguate must remain
unchanged through that read. It never selects the first same-size window.

## Environment

- `ASTRA_COMPUTER_HELPER_PATH`: absolute development/test override for the signed helper. Unsafe ownership, symlinks, relative paths, and non-executable targets are rejected.
- `ASTRA_COMPUTER_CACHE_ROOT`: local root for private Computer Use session directories. The runtime creates and validates private children beneath it.
- `ASTRA_MACOS_COMPUTER_E2E`: real desktop tests run only when this is exactly `1`; unset or `0` produces an explicit skip.
- `ASTRA_MACOS_APPSHOT_E2E`: the opt-in native Edge Appshot capture test runs only when this is exactly `1`; the user must make the intended Edge source window frontmost before the run.
- `ASTRA_COMPUTER_DEBUG_RETAIN`: not implemented. Captures are always removed; setting this variable, including to `0`, has no effect. This is intentional until a retention mode can preserve the request-local privacy contract.

## Real acceptance

The suite creates all fixture files in a fresh mode-0700 pytest directory. It never opens or modifies existing user documents. WPS Save As and PDF export, Finder copy/rename/trash denial, and Terminal `printf` are restricted to that directory.

The supported native unit/harness gate runs Swift Testing directly and rejects
zero discovered tests:

```bash
bash scripts/test_macos_computer_helper.sh
```

```bash
bash scripts/build_macos_computer_helper.sh
ASTRA_MACOS_COMPUTER_E2E=1 .venv/bin/python -m pytest tests/macos_computer_e2e -v
```

Without the environment switch, the suite reports why it was skipped. With it enabled, a missing application or permission is a failure with an actionable diagnostic, not a pass. Compatibility cells are reported independently for AppKit controls/canvas, WPS, Electron, and Chromium; a failure or unavailable application is never converted into another cell's pass.

The checked-in deterministic suite is the acceptance gate for protocol, approval, overlay, cache, and display-capture behavior. A real WPS run is counted only when the current signed helper creates a new test-owned DOCX and searchable PDF through exact approvals in that run. A skipped run, an earlier output, or a fail-closed stop is not acceptance. The current PID compatibility evidence is recorded in the [2026-08-29 compatibility report](macos-computer-compatibility-evidence.md); deterministic results and real-desktop results are reported separately.

The separate [Smart Snapshot read-only WPS runbook](macos-smart-snapshot-runbook.md) measures whether the currently reported WPS AX subtree is useful for visible and offscreen text without scrolling, focus changes, or input. It is not an implementation-validation gate and cannot promote `coverage=reported_ax_subtree` to a completeness claim.

The checked-in compatibility registry contains exact bundle/version/backend/action capabilities. Read the current file and compare its hash with the signed helper resource; an older empty-registry report is historical evidence. A configured cell does not prove that this build passed a fresh real-app workflow. Keep deterministic harness results, configured routes and current app-effect acceptance separate, using the [evidence and routing guide](computer-use-evidence.md). Never infer a version or enable a whole application from one passing action.

### Unavailable target-window pixels

`window_content_unavailable` means the exact-window image was fully transparent. The
transaction publishes neither a snapshot nor action authority. It is not a generic
`snapshot_failed`, and it does not prove occlusion, missing focus, or input failure.
Some applications withhold capture content even for on-screen windows. Do not switch
to desktop-region screenshots, repeatedly activate/move the target, or replay input.
Ask the user to inspect the application's capture/privacy state; after a relevant
state change, refresh `computer_apps` and bind the current window.

Welcome-to-main transitions can replace the OS window while retaining its title.
Use the returned transition's next-observation refs or refresh the catalog and explicitly
select the new window. Shell `--first`, historic sizes and old coordinates do not bind
the new target. Focus observed after takeover cleanup is not input-delivery evidence.

## Checked form batches (phase one)

Use `computer_apps` → `computer_get_app_state` → one `computer_act` with independent `{type:"click", element_index:currentIndex, checked:true}` goals. The default mode is `auto`: new helpers negotiate and plan foreground takeover internally before dispatch. Legacy helpers retain background behavior; explicit modes remain supported. Never pass a background plan to takeover_begin.

Protocol remains v4. Capabilities `subtree_v1`, `checked_click_v1`, and `auto_takeover_v1` live inside the extensible AX tree. Old clients can consume ordinary snapshots; new clients do not send optional requests to old helpers. Native `snapshot_subtree` binds the exact old snapshot and AX object to the same window, then re-reads it. A single truncated web form triggers one anchored read during initial observation and checked-batch verification. No unbounded expansion or title-based recovery occurs.

Each checked goal reads first, skips an already satisfied control, otherwise dispatches once and waits at most 400 ms for its AX state. Final `choice_verification` correlates fresh observations with process-scoped AX identities, role and label/title, surviving layout changes and text-context truncation. Correlation tokens cannot authorize actions. Unknown state never means false or permits replay. Numeric 0.0/1.0 and boolean values are recognized; long choice labels are retained within the existing byte bound.

Coordinates default to window-local logical units. `coordinate_space="image_pixels"` uses the exact `published_image_size` of the latest target-window screenshot. Display images and stale geometry cannot authorize these actions. Durable request-local receipts contain only fixed dispatch/verification enums and counts, never page text, paths, refs, screenshots or input.

The macOS `WindowSharingSessionButton` badge is recognized using its complete nonmodal AX shape and titlebar location. It is excluded only from dialog classification; ordinary dialog handling and raw pointer hit testing remain intact.

## Targeted subtree observations

Use `subtree_ref` from a fresh snapshot to expand one observed subtree when a
large Accessibility tree hides relevant controls. `role_filter` narrows the
returned roles; it does not make an unseen target actionable. New snapshots
produce new refs, and unmatched roles, traversal limits or exhausted budgets
must remain explicit. Snapshot expansion is read-only and is not proof that all
application content was observed.

## Input delivery contracts

Compatibility cells can declare `requires_active` for applications that require
activation before synthetic pointer input. The planner rejects background
pointer delivery for such a cell before input starts and requests the existing
foreground-takeover flow. A denied activation does not authorize background
fallback. Missing or false values do not grant an otherwise unsupported action.

Keyboard focus acquisition must establish the exact target window and an
eligible, non-secure input element. For foreground takeover, acquire and verify
focus after activation and immediately before delivery; do not demand the future
active focus as a prerequisite to activating the application. Frontmost PID alone
is insufficient. A changed, ambiguous or protected target stops delivery.

Plain text and physical key chords use distinct delivery plans. Unicode text
avoids treating ordinary characters as shortcuts; physical keys retain their
actual key semantics. Input-method composition requires its own evidence and
cannot be treated as universally supported because one application passed a
text fixture. A partially delivered or uncertain action must not be replayed
through another input route. Check fresh state before a new explicit action.
