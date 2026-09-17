# Live foreground identity for the macOS helper

Approved as part of the 2026-09-17 browser-upload work. The user reported that
file selection works once focused, but Astra cannot acquire that focus itself.

## Cause and decision

The synchronous, long-lived helper used `NSWorkspace.frontmostApplication` for
activation, input guards, window mapping and cleanup. AppKit updates this value
through the main run loop. Repeated reads in the helper could retain an earlier
application even after the real foreground changed. A probe reproduced this:
the long-lived process retained WPS while a fresh process observed Finder.
Production CU reproduced a background file panel while incorrectly assuming
Edge was already active. This is not evidence that macOS prohibits activation.

Replace input-authority reads with a fresh system-wide Accessibility
`kAXFocusedApplicationAttribute` query. Bound each query to 200 ms and any outer
observation deadline; bypass observation attribute caches. Require a valid AX
object and positive PID. Failed reads provide no foreground authority and never
fall back to the AppKit cache. Retain existing activation, exact window, overlay,
takeover, cancellation and physical-user-input protections. Recorder/Appshot
processes with their own run loops are outside this repair.

Sources: [NSRunningApplication lifecycle](https://developer.apple.com/documentation/appkit/nsrunningapplication)
and [AX focused application](https://developer.apple.com/documentation/applicationservices/kaxfocusedapplicationattribute).

## Acceptance

- Malformed, missing or failed AX results fail closed; each new PID is used.
- Existing foreground activation/input and restoration tests remain applicable.
- Prime the production helper while Edge is active, put Finder in front, then
  issue an explicitly authorized CU foreground action. Verify actual Edge
  foreground from an independent fresh process, bind the file panel, select a
  neutral local fixture and read its filename back from the page. No human focus
  click and no form submission are part of this test.

The new Finder-starting real Edge case passed on 2026-09-17. It exercises the
same long-lived process/cache condition that failed before the change. The
separate `browser_upload` path avoids native focus entirely.

## Default file-picker routing

A subsequent default `auto` test acknowledged a background AXPress, then left
the file panel unbindable. A file input with an AXPress action is not evidence
that its system dialog can be used in the background. The native planner will
require takeover for a complete `AXButton` / `AXFileUploadButton` identity, before
dispatch. The existing `auto` planning and approval path can then activate the
exact window before issuing the single AXPress. Explicit background requests
return the existing pre-input takeover requirement. Other buttons keep their
existing routes; labels or localized wording do not establish file-picker
identity. Custom controls without this identity may still require explicit
foreground takeover. Already-open unmatched panels remain protected.

Add planner tests proving no background dispatch, and real Edge tests using a
visible native file input with default auto while Finder is initially active.
Retain the custom-button explicit-takeover and image-pointer cases. Verify
foreground after opening, panel binding, path entry and selected filename.

Identity reference: [HTML Accessibility API Mappings, file upload input](https://w3c.github.io/html-aam/#input-type-file).

Real Edge inspection found a native file input reported as an ordinary AXButton
with no subrole. The standardized identity above is therefore an optional
optimization, not sufficient for Chromium. Add an `opens_dialog` boolean to
`computer_act`: when the intended action opens a native file/save/modal panel,
set it on the first click. Default auto then plans foreground takeover directly,
before any input, using the existing exact-batch approval and activation guards.
Explicit background plus this flag is rejected before planning. The flag does
not identify or authorize any window; refs, targets and approvals remain exact.
It never retries a click or repairs an already-unmatched panel. Skills and tool
descriptions must explain this intent for native and custom picker buttons.

Test the default auto call with this intent against the real Edge file input
from a different foreground application, and cover denied approval and
contradictory explicit background mode without dispatch.

The dialog-intent real Edge case passed on 2026-09-17: an ordinary AXButton
without a subrole, Finder initially in front, auto plus opens_dialog, exactly
one opening click, Edge independently observed in front, bindable file panel,
verified path entry and selected filename. No human focus click or form
submission occurred. Python tests also verify denied approval dispatches
nothing and explicit background with the flag is rejected before planning.
