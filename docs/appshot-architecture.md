# Appshot architecture

Appshot captures one selected foreground window and offers its screenshot and
available interface text to an Astra session draft. Capture is explicitly enabled
by the user. Accepting an attachment into the draft does not send it to a model;
the user submits the message separately.

For Windows installation, commands and test switches, use the
[native adapter guide](../native/windows-computer-helper/README.md). Mac setup
and input-context boundaries are covered by
[macOS Computer Use](macos-computer-use.md#appshot-source-binding).

## Components

| Component | Responsibility |
| --- | --- |
| `native/appshot-core/` | Shared Swift models, receiver selection, bounded state and attachment transactions |
| `native/macos-computer-helper/` | Mac capture, Accessibility, permissions, hotkeys and POSIX transport/storage |
| `native/windows-computer-helper/` | Windows capture, UI Automation, hotkeys, named pipes and native file/peer verification |
| `ui-tui/` | Receiver state, draft attachments and explicit submission |
| `agent/runtime/` | Independent attachment admission and session-owned media |

Shared logic does not make platform security evidence interchangeable. Mac keeps
wire v1 and its existing executable entry points. Windows uses explicit v2
payloads, process creation identity, user/session identity and native file IDs
with ACL checks. The consumers distinguish the protocol versions instead of
pretending that Windows has POSIX ownership and inode semantics.

## Capture and delivery

1. An enabled shortcut starts a bounded capture for an eligible receiver and one
   exact target window. A target change cannot silently select another window.
2. The platform adapter publishes bounded artifacts in private runtime storage.
   Unavailable interface text may produce a screenshot-only result with a reason.
3. Offer, acknowledgement, commit and receipt are separate steps. Disconnect,
   expiry, disable and cancellation invalidate unfinished delivery; an ordinary
   state update is not a commit receipt.
4. Explicit message submission passes through Python admission. Sent media is
   stored with the session so reload does not depend on broker temporary files.

Consumer parsing alone is not file authority. Platform checks validate peer
identity, storage ownership, file identity and bounded reads. Cleanup preserves
replacement files. Unknown crash residue requires inspection; it is not an
invitation to delete arbitrary files. The Windows adapter rejects late capture
results and bounds its capture workers independently of the broker event loop.

## Maintenance and verification

Shared Swift tests, platform-native tests, TUI tests and Python admission tests
cover different parts of the path. A fixture pass is not evidence that a user's
installed helper can capture their current application.

On macOS, shared/native package tests are available through the release gate's
`--native` option, with separate temporary build directories. The Windows guide
lists the corresponding PowerShell scripts and explicit live-capture switches.
Keep exact OS/build identities and single-run output in a private test directory.
Appshot support does not enable general Windows mouse/keyboard Computer Use.
