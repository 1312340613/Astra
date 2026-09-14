# Windows Appshot native adapter

Windows Appshot now connects the native broker, TUI draft, independent Python
admission and session-owned media. `--service-info` reports `production_ready: true`
for Appshot v2; this does **not** enable other Windows Computer Use capabilities.
An interactive TUI starts the local service, but capture remains **disabled by default**.
Captures enter the draft only. Sending requires an explicit user submission.

## Install and use

Build/install the helper and its app-local runtime DLLs from the source checkout:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_windows_computer_helper.ps1 -Configuration release -Install
```

This installs only `.astra/bin/AstraWindowsComputerHelper/` under the installation
root; it does not replace the shared Astra installer, model settings or macOS bundle.
A previous helper bundle is retained with a `.previous-` suffix for rollback.
Close Astra before replacing a loaded helper. Compiler tools are build dependencies,
not a requirement for running the installed bundle.

Restart Astra, run `/appshot enable`, then press an arrow key in the receiving
TUI, switch to the target app and press **Ctrl+Shift+Z**. Add your question and
press Enter to send. `/appshot status`, `/appshot disable`, and
`/appshot shortcut Ctrl+Alt+Z` manage the service locally. No helper window appears
during normal startup. Mac Terminal's focus probe is not used on Windows.

Discovery respects the absolute `ASTRA_COMPUTER_HELPER_PATH` override or the fixed
bundle beneath `AGENT_PROJECT_ROOT`, never the current working directory/PATH.
An override must include `AstraWindowsNative.dll` and runtime DLLs beside the exe.
Runtime artifacts use the native current-user-private `%LOCALAPPDATA%/AstraAppshot`
directory. Only one live broker may own it; another Windows logon cannot take over
its live descriptor. Pipe transport additionally isolates the Windows session.

Sent images are stored under `<session>.appshot-media`, using the same native
ACL/file-ID/no-reparse backend, exclusive publication, and receipt-bound rollback.
Reload, rename and delete do not depend on the broker's temporary artifacts.
Switch to another session before deleting the active session.

## Implemented

- Swift orchestration using the shared `../appshot-core` package; macOS retains
  its executable/package entry points and wire v1.
- A C ABI bridge for Windows Graphics Capture, UI Automation, kernel process
  identity (PID + creation time + SID), and hidden job-owned worker processes.
- One exact target window; no full-desktop, PrintWindow or unrelated-window fallback.
- A five-second shared deadline, shorter UIA budget, screenshot-only fallback for
  unavailable text, and cancellation/late-result rejection.
- Bounded worker output and memory, one process per job, and kill-on-job-close.
  UIA password controls are not read; screenshots may still contain visible secrets.
- Explicit Windows wire v2 parsers and shared valid/invalid fixtures in Swift,
  TypeScript and Python. Parsed manifests are **not** artifact authority.
- Handle-pinned private storage: fixed-drive canonical paths, every ancestor held
  against rename, current-user owner and protected DACL, volume/file ID, link count,
  bounded reads and SHA-256. Exclusive publication uses handle-relative native
  rename, not an absolute-path fallback. Cleanup preserves replacement files.
- A Swift artifact producer/verifier and a kernel-parent-bound read bridge, verified
  using a real self-owned foreground window. Synthetic end-to-end tests separately
  exercise the real TUI client, Python admission and durable session lifecycle.
- Private per-user/per-Windows-session named pipes, kernel-proven peer creation
  identity, independently checked pipe DACLs, an exclusive broker election,
  bounded queues/connections, and cancellation that invalidates the old channel.
  The native test uses a real helper child; it is not a different-user token test.
- Strict v2 discovery/settings and a Swift broker using the shared registry.
  A helper relay may claim only its independently kernel-proven parent. Capture
  selection, offer/ack/commit/receipt, release, disconnect and expiry retain the
  original transaction boundaries; a normal state update is not a commit receipt.
- Message-window hotkeys (no keyboard hook or synthesized input), disabled by
  default. A conflicting replacement retains the old registration; disabling or
  closing unregisters the owned shortcut. Settings survive a normal restart.
  A crash during settings replacement can leave settings absent, which defaults
  to disabled; this is not advertised as an atomic settings replacement.
- A single serial capture/publication/cleanup owner off the broker event loop.
  Cancellation rejects late delivery immediately; new captures are refused until
  in-flight work and cleanup finish. The WGC/UIA child processes are job-owned and
  deadline-controlled. Filesystem calls remain cooperative, not hard-preemptible.
  Directory quotas bound admitted files/bytes; unexplained crash-left artifacts
  stop a new producer without guessing ownership or deleting them. Failed storage,
  publication or cleanup stops that worker from accepting further captures;
  restarting requires a clean, independently revalidated runtime directory.

## Build and test

Verified on x64 Windows with Swift **6.3.3**, Visual Studio Build Tools **17.14.40**
(MSVC **14.44.35207**) and Windows SDK **10.0.22621.0**. Swift's installer also
installs its Python 3.10 prerequisite. These are developer dependencies only;
the build script copies their runtime DLLs into the helper bundle for standalone use.

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_appshot_core.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_windows_computer_helper.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_windows_computer_helper.ps1 -Configuration release -SkipBuild -Files -IPC -Hotkey -Broker
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_windows_computer_helper.ps1 -Configuration release -SkipBuild -Consumer -Daemon
```

Use `-Capture` alone for a visible, self-owned test window. The basic capture test
verifies readable UI text and omission of its password field, prints only
dimensions/byte counts, and neither persists nor uploads the capture.
Do not switch foreground windows while this opt-in test runs: a target change
must fail, not silently capture another app.

Add `-Files` for real filesystem security fixtures (no screen access). It tests
private DACLs, exclusive publication, hashes/IDs, read limits, writer/delete
exclusion, hardlinks, a self-owned directory junction and replacement-safe cleanup.
`-Files -Capture` additionally tests a temporary PNG/UIA/manifest bundle and the
child read bridge; only the self-created test window can be captured. The prompt
is shown above other windows without stealing keyboard focus. Click **Start test**
within 60 seconds and keep it in front until it closes. Only then does the normal
five-second capture budget start. Closing the prompt cancels the test. It does not
synthesize input or capture a different app.
Successful file tests remove their files and empty test directories. Failed
fixtures are retained for inspection instead of recursively deleting unknown data.

`-IPC` verifies real parent/child transport, peer identity, actual pipe DACL,
exclusive election, framing bounds, connection limits, cancellation and restart.
`-Hotkey` verifies registration conflicts and cleanup. `-Broker` uses real IPC
but **synthetic attachments** to validate commands, private discovery/settings,
nonce rejection, exact-once transactions, timeout/disable/disconnect cleanup,
relay parent proof and safe restart/refusal. These tests do not read a window.
Hotkey fixtures temporarily use rare `Ctrl+Alt+Shift+F22/F23/F24` chords and release
them on exit; an existing registration is never taken over.

`-Broker -Capture` adds an opt-in real capture/worker/broker/read-bridge transaction.
It uses the same manual Start test guard, only captures that exact self-owned
window, and also tests cancellation of a published but undelivered result.
Its consumer is a protocol fixture, **not** the production TUI or session backend.
When combined with `-Files`, this replaces the separate artifact capture test so
only one manual test prompt is needed. Successful runs delete their temporary
PNG/UIA/manifest files; no mode uploads them.

Native worker modes are internal implementation details, not supported user commands.
The default Astra shortcut remains `Ctrl+Shift+Z`; these tests do not register it.
`-Consumer` tests explicit synthetic submission, replay, cancellation, failed-launch
rollback, reload after broker cleanup, session rename and deletion. It makes no
model requests. `-Daemon` tests the real production launch/restart/idle shutdown
with compiler paths removed, default-disabled capture, and no screen access.

Earlier third-increment validation: 9 shared Swift tests pass in debug/release; Windows debug/release
builds and controlled capture/worker checks pass. TUI Appshot tests: 101 pass, 4
platform skips, with a successful TypeScript check/build. The unfiltered Python
Appshot selection has 321 passes, 40 platform skips and one failure in an unchanged
8192-context local Qwen profile: the existing 8192 minimum output reservation leaves
zero input capacity. Excluding that profile gives 318 passes, 40 skips, 4 deselections.
No model profile or budget policy was changed to hide this failure.

The concurrent macOS source-launcher changes through `0bddb52` were merged and
reviewed: `AGENT_PROJECT_ROOT` continues to identify the installation, separate
from the selected workspace, and the existing helper override is retained.
The Windows BAT wrapper now preserves child exit codes even when the updater
rewrites that wrapper. Launcher/foundation regressions: 70 pass, 9 platform skips;
Appshot plus TUI-settings regressions: 102 pass, 4 skips.

An older local checkout may report an unverified environment in `astra doctor`
after adopting the new launcher. Its supported migration is `astra setup`;
the Appshot build scripts do not forge that record, reinstall the Python/Node
environments, or register a global Astra command.

## Remaining acceptance and limitations

1. Automatic crash-left artifact custody recovery is not implemented. Unknown
   residue is preserved and startup fails closed; it is never guessed/deleted.
2. The actual window capture and the synthetic TUI/admission tests are separate;
   a fresh manual full production-TUI screenshot test remains useful after installation.
3. Actual macOS native compile/tests and capture smoke test. Windows shared tests
   and source review do not substitute for macOS validation.

On the Mac checkout, run both packages before treating the extraction as accepted:

```sh
swift test --package-path native/appshot-core
swift test --package-path native/macos-computer-helper
```

The existing Phase T `--native` gate now runs the shared package before the Mac
package, serially and with separate temporary build directories.

The existing macOS capture smoke test still requires a real desktop and its
normal permissions; successful unit tests alone do not prove that capture works.

Local validation after standard `astra setup --repair` (2026-09-12): doctor reports
healthy with no pending update, the existing `.env` hash is unchanged, launcher/
foundation/gate/documentation tests pass (94 passed, 9 platform skips), and TUI
Appshot/settings tests pass (102 passed, 4 skips). The scoped Python Appshot run
still passes 318 with 40 skips and the same four explicit profile deselections.
Real filesystem checks and the release capture/storage integration now pass.
The earlier prompt could remain obscured and timed out after ten seconds; a
`WS_VISIBLE` flag alone did not prove that the user could see it. The opt-in test
now shows a topmost prompt without taking keyboard focus and requires a manual
Start test click. The successful run verified a 782x516 PNG, readable UIA text
with the password value absent, private PNG/UIA/manifest publication, verified
in-process and parent-bound child reads, wrong-session rejection, replacement
preservation, cancellation, partial-timeout cleanup and empty-directory cleanup.
No user application was captured, and no fixture media remains after success.
That second-increment run did not exercise a broker, hotkeys, TUI draft delivery
or session media; macOS native acceptance remains outstanding.

Third increment (2026-09-12): Windows debug/release builds and the non-capture
IPC/hotkey/files/service suites pass. The first real broker/capture attempt did
not pass and had only a generic diagnostic. The user confirmed clicking Start;
the earlier failure cannot be attributed to missing input or any specific cause.
No media remained in that fixture directory. Stage diagnostics now distinguish
an unstarted/closed fixture from capture, transport and storage failures.

A subsequent release `-Broker -Capture -SkipBuild` run passed after the manual
Start click: 782x516 real capture, readable UIA with the password value absent,
independently verified child read, offer/ack/commit/incorporation receipt, release,
serial-worker cleanup and cancellation of a published but undelivered result.
The runner confirmed all fixture artifacts and both empty test directories were
removed. This accepts the real broker/capture composition, not the still-pending
production TUI/session-media integration, and does not diagnose the earlier failure.
Shared Swift: 9 debug and 9 release tests; Python Appshot selection: 318 passed,
40 platform skips, 4 explicit `uncensored` profile deselections as noted above;
TUI Appshot/focus/rejection/settings: 116 passed, 4 platform skips, build passed.
This increment changes no Mac native sources, TUI runtime guard or model profile.
