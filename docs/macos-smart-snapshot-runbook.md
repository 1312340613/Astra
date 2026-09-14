# macOS Smart Snapshot read-only WPS measurement

**Status:** NOT RUN

## Purpose and result boundary

This runbook measures whether the Accessibility subtree currently reported by
WPS contains useful test-owned text that is visible in the selected viewport
and test-owned text that is known to be offscreen. It does not validate the
Smart Snapshot implementation, grant production compatibility, or prove that
WPS exposes a complete document.

The only valid coverage value is `reported_ax_subtree`. Finding the offscreen
marker is useful positive evidence for this exact WPS build, document, window,
and viewport. Not finding it may reflect WPS virtualization or AX omission.
Neither outcome changes the coverage contract, and `truncated=false` does not
mean complete coverage.

This procedure is intentionally not executed as part of Task 6. Task 6 permits
only deterministic non-live verification; it must not open WPS, request macOS
permissions, capture a screen, move focus, scroll, or deliver input.

## Safety rules

During the measured sequence:

- do not call `computer_apps`, `computer_focus`, `computer_act`,
  `computer_handoff`, or `computer_resume`;
- do not scroll, type, click, select, resize, move, activate, minimize, or
  otherwise mutate WPS or its document;
- do not use pointer, keyboard, clipboard, AppleScript UI control, synthetic
  input, or a compatibility-registry entry;
- do not accept a macOS Accessibility or Screen Recording permission prompt;
- do not use a sensitive or existing user document; and
- stop immediately if the exact WPS PID, window, frontmost state, focused AX
  identity, bounds, or viewport changes.

The exact once-only text observation approval is allowed only through a
nonactivating approval surface that leaves WPS frontmost and preserves the
focused AX identity. If the current approval surface cannot prove that
postcondition, classify the run `NOT_RUN` rather than changing focus or trying
a workaround.

## Prerequisites prepared before the measurement

1. A newly created, test-owned WPS fixture already contains two unique,
   nonsensitive ASCII markers: one on the initially visible page and the other
   several pages below it. Example forms are
   `ASTRA_SMART_VISIBLE_<random>` and `ASTRA_SMART_OFFSCREEN_<random>`.
2. Sidebars, search panels, dialogs, comments, and popovers are already closed
   before the run. The visible marker is plainly inside the fixed viewport and
   the offscreen marker is not.
3. The current release helper was built with the deterministic commands in
   [Non-live acceptance](#non-live-acceptance), and that exact signed helper
   already has Accessibility and Screen Recording permission. A missing
   permission makes the run `NOT_RUN`; do not request it here.
4. A fresh Computer Use session was created outside this run and is already
   bound to the exact WPS application/window. WPS is frontmost with focus on
   the stable document view. The temporary lab note already records the WPS
   bundle ID, exact version, PID, window identity, document fixture hash, and
   starting viewport without recording document text.
5. The current Computer Use session has no earlier Smart Snapshot detail
   artifacts, is not poisoned or awaiting cleanup, and has capacity for two
   PNGs and one AX JSON.

If any prerequisite is missing, leave the status `NOT RUN`. Setup activity is
not measurement evidence.

## Repository support and exact invocation

The repository exposes Smart Snapshot through the model-facing
`computer_snapshot` tool. It does not currently ship a standalone command-line
driver for a live tool invocation. Use these exact tool arguments; do not
substitute an ad hoc Python manager or native-helper call, because that would
bypass the public approval and request-local result path.

First, read the bounded target state without activation:

```json
{"tool":"computer_status","arguments":{}}
```

Require the existing target to be the prepared WPS window and
`handoff_active=false`. Do not call `computer_focus` to repair a mismatch.

Capture the unchanged default/off control:

```json
{"tool":"computer_snapshot","arguments":{"scope":"target_window","text_detail":"off"}}
```

Require a fresh target-window image whose visible viewport contains the visible
marker and not the offscreen marker. The result must not contain
`text_detail_path`, `text_detail_metadata`, or `text_summary`, and no `.ax.json`
entry may be created.

Without touching WPS, request the detail measurement:

```json
{"tool":"computer_snapshot","arguments":{"scope":"target_window","text_detail":"on"}}
```

Approve exactly once for the exact session, WPS application/window target,
`target_window` scope, detail mode, and focus generation. The grant must be
consumed before capture is awaited. Do not reuse a previous display approval or
action approval. For a separate display experiment, use the exact combined
request below and expect one combined approval, never two grants:

```json
{"tool":"computer_snapshot","arguments":{"scope":"display","text_detail":"on"}}
```

The display variant is optional and is not part of the WPS offscreen verdict.
It grants no pointer or keyboard authority and cannot change the compatibility
registry.

## Artifact and result checks

For the `on` target-window result, require all of the following before using the
text as evidence:

- `image_paths` contains one absolute request-local PNG path;
- `text_detail_path` is an absolute request-local path ending in `.ax.json`;
- both paths have the same exact parent directory, which is an owned,
  non-symlink directory with mode `0700`;
- the PNG and AX JSON basenames are
  `snapshot-<same-lowercase-32-hex-token>.png` and
  `snapshot-<same-token>.ax.json`;
- `text_detail_metadata.coverage` is exactly `reported_ax_subtree`;
- metadata has schema version 1, the current snapshot ID, node/depth/byte/hash
  values, and consistent truncation fields;
- the complete `root` is absent from the inline result;
- `text_summary` is the only inline text detail and remains within 200 entries
  and 32 KiB, including an explicit truncation marker when needed; and
- the ordinary bounded inline `ax_tree` remains a separate field.

Before `computer_close`, an operator may use the following exact read-only
check from the repository root. It is permitted only through an already
provisioned, nonactivating, history-disabled, nondurable in-memory checker
channel that neither persists nor replays command text, environment values,
marker strings, or raw output and that cannot activate Terminal or change
focus. If that channel is unavailable, record `NOT RUN`; do not open or repair
an interactive shell. The displayed shell block is the exact checker
computation, not authorization to establish a new command channel. Copy values
only from the current request-local result. The checker imports the production
strict decoder, reads but does not modify either artifact, and prints only
bounded booleans and counts.

```bash
export SMART_PNG_PATH='/absolute/request-local/snapshot-<token>.png'
export SMART_AX_PATH='/absolute/request-local/snapshot-<same-token>.ax.json'
export SMART_SNAPSHOT_ID='<snapshot_id>'
export SMART_METADATA_JSON='<exact text_detail_metadata JSON object>'
export SMART_VISIBLE_MARKER='ASTRA_SMART_VISIBLE_<random>'
export SMART_OFFSCREEN_MARKER='ASTRA_SMART_OFFSCREEN_<random>'
.venv/bin/python - <<'PY'
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from agent.runtime.computer_protocol import decode_text_detail_envelope

png_path = Path(os.environ["SMART_PNG_PATH"])
ax_path = Path(os.environ["SMART_AX_PATH"])
snapshot_id = os.environ["SMART_SNAPSHOT_ID"]
metadata = json.loads(os.environ["SMART_METADATA_JSON"])
visible = os.environ["SMART_VISIBLE_MARKER"]
offscreen = os.environ["SMART_OFFSCREEN_MARKER"]

assert png_path.is_absolute() and ax_path.is_absolute()
assert png_path.parent == ax_path.parent
parent_info = png_path.parent.stat(follow_symlinks=False)
assert stat.S_ISDIR(parent_info.st_mode)
assert stat.S_IMODE(parent_info.st_mode) == 0o700
assert parent_info.st_uid == os.getuid()

png_match = re.fullmatch(r"snapshot-([0-9a-f]{32})\.png", png_path.name)
ax_match = re.fullmatch(r"snapshot-([0-9a-f]{32})\.ax\.json", ax_path.name)
assert png_match and ax_match and png_match.group(1) == ax_match.group(1)
file_stats = []
for path in (png_path, ax_path):
    info = path.stat(follow_symlinks=False)
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == os.getuid()
    assert info.st_nlink == 1
    file_stats.append(info)
assert all(info.st_dev == parent_info.st_dev for info in file_stats)
assert file_stats[0].st_ino != file_stats[1].st_ino

data = ax_path.read_bytes()
digest = hashlib.sha256(data).hexdigest()
document = decode_text_detail_envelope(
    data,
    snapshot_id=snapshot_id,
    metadata=metadata,
    sha256=digest,
)
encoded_tree = json.dumps(document["root"], ensure_ascii=False)
print(json.dumps({
    "schema_valid": True,
    "same_token": True,
    "coverage": document["coverage"],
    "truncated": document["stats"]["truncated"],
    "truncation_reasons": document["stats"]["truncation_reasons"],
    "node_count": document["stats"]["node_count"],
    "visible_marker_reported": visible in encoded_tree,
    "offscreen_marker_reported": offscreen in encoded_tree,
}, sort_keys=True))
PY
```

The production decoder checks strict UTF-8/JSON, duplicate and unknown keys,
exact limits, finite geometry, node/depth counts, snapshot ID, SHA-256,
truncation ordering, and secure/indeterminate redaction. This lab read is
supplemental evidence; Python already performs the same defensive validation
before exposing `text_detail_path`.

## Fixed implementation limits to record

The artifact must declare depth 20, 4,000 nodes, 4 KiB structural strings,
256 KiB values, 4 MiB aggregate text, 8 MiB final JSON, and a 5-second traversal
budget. The result summary is limited to 200 entries and 32 KiB. Session detail
quota is eight artifacts and 64 MiB. A limit hit yields a valid partial artifact
with `truncated=true` and ordered exact reasons; it never upgrades coverage.

Native redaction must precede persistence. Secure or indeterminate role/subrole
removes value, selected text, ranges, and attributed text. Canonical
`AXUnknown` in either field is indeterminate; an incomplete role is emitted as
`AXUnknown`, an incomplete subrole is omitted, and neither state may run a
sensitive accessor. Python rejects any invalid redaction state. The artifact
must contain no actionable `element_ref`. Native traversal applies the positive
remaining-time AX messaging timeout to the exact element before every actual
accessor and restores inheritance afterward; failure to establish or restore
that boundary fails closed.

## Verdicts

Use exactly one measurement verdict:

- `PASS`: both synthetic markers are reported by the verified artifact, the
  offscreen marker was absent from the unchanged viewport, no truncation
  explains the result, and every focus/mutation/artifact/cleanup check passed.
  This means **useful offscreen evidence for this one run only**.
- `WARN`: the verified artifact reports the visible marker but not the known
  offscreen marker, or a declared limit makes the usefulness result
  inconclusive. This is compatible with `reported_ax_subtree` and is not an
  implementation failure.
- `FAIL`: the measured sequence changed focus or target state, mutated WPS,
  leaked a secure/indeterminate value, exposed the full tree inline, violated
  pair/schema/hash/bounds semantics, left an unsafe cleanup state, or failed to
  report the visible marker without an exact bounded explanation. Treat this
  as a safety or contract symptom requiring investigation, not as permission
  to weaken validation.
- `NOT RUN`: prerequisites were unavailable, permissions would need prompting,
  exact target/focus could not be preserved, a nonactivating approval path was
  unavailable, or no fresh run was completed.

No verdict from this runbook is implementation acceptance. The deterministic
non-live gates below validate the implementation; the runbook only measures
real WPS usefulness.

## Cleanup and bounded evidence capture

While the same request-local session is live, record only a sanitized lab row:
commit, helper code identity, macOS and exact WPS versions, test-fixture hash,
scope, approval kind, unchanged-target boolean, off-mode absence boolean,
same-token/schema/hash booleans, `coverage`, truncation state/reasons, and the
two marker-present booleans. Do not persist the artifact path, raw AX JSON,
image, inline AX tree, summary text, marker strings, document text, or helper
diagnostics in durable conversation or UI events.

Then invoke cleanup exactly once:

```json
{"tool":"computer_close","arguments":{}}
```

Require the close result to report success, then use the nonactivating cached UI
command `/computer status` to confirm that Computer Use is closed; do not call
the model-facing `computer_status`, because that would query the helper again.
Do not remove artifacts by pathname. Normal cleanup uses the held directory FD
and lease and preflights both saved pair authorities before the first unlink.
If an entry is missing, replaced, relinked, permission- or size-changed, or
otherwise mismatched during pair preflight, cleanup must unlink neither member,
poison/block reuse, and require manual recovery. After a successful preflight,
cleanup revalidates and unlinks the two names sequentially; a later I/O or
revalidation failure may leave one member and must poison/block reuse until
terminal cleanup or explicit recovery. Temporary, quarantine, unknown, or
uncertain residuals are also manual-recovery cases; never delete them ad hoc.

## Non-live acceptance

These commands validate the implementation without starting a live WPS or
permission workflow:

```bash
swift test --package-path native/macos-computer-helper
ASTRA_MACOS_COMPUTER_HELPER_HARNESS=1 \
  swift run --package-path native/macos-computer-helper \
  AstraMacComputerHelperHarness
.venv/bin/python -m pytest \
  tests/test_computer_commands.py \
  tests/test_computer_policy.py \
  tests/test_computer_registration.py \
  tests/test_computer_backend.py \
  tests/test_computer_protocol.py \
  tests/test_computer_tools.py \
  tests/test_computer_use_skill.py \
  tests/test_macos_computer_helper_integration.py \
  -q
.venv/bin/python -m ruff check \
  agent/runtime/computer_backend.py \
  agent/runtime/computer_protocol.py \
  agent/runtime/macos_computer.py \
  agent/runtime/computer_policy.py \
  agent/runtime/tools/computer.py \
  tests/test_computer_backend.py \
  tests/test_computer_protocol.py \
  tests/test_computer_registration.py \
  tests/test_computer_policy.py \
  tests/test_computer_tools.py \
  tests/test_macos_computer_helper_integration.py
bash scripts/build_macos_computer_helper.sh
codesign --verify --deep --strict --verbose=2 \
  .astra/bin/AstraMacComputerHelper.app
nm -u .astra/bin/AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper \
  | python3 scripts/check_macos_computer_helper_symbols.py
cmp config/macos_computer_compatibility.json \
  .astra/bin/AstraMacComputerHelper.app/Contents/Resources/macos_computer_compatibility.json
git diff --check
```

The direct Swift commands run the complete unit suite and native production
harness without starting the cursor-sidecar visual smoke. The broader
`scripts/test_macos_computer_helper.sh` wrapper additionally starts that
sidecar and therefore requires separate explicit live-UI authorization; it is
not part of this non-live procedure. The release build script also performs
strict codesign, forbidden test-marker checks, symbol validation, and
compatibility-resource validation before replacing the bundle. The explicit
identity commands make those final bundle properties independently visible in
the evidence log.
