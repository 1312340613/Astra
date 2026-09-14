#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
package_root="$project_root/native/macos-computer-helper"
production_source="$package_root/Sources/AstraMacComputerHelper"

if rg -n 'CGWarpMouseCursorPosition|CGSyntheticInputPoster|legacyForegroundActionAllowed' "$production_source"; then
  printf '%s\n' 'Legacy global input capability remains in production source.' >&2
  exit 1
fi

# Match the release gate: only the existing guarded foreground emitter may post globally.
if rg -n '\.post\(tap:' "$production_source" --glob '!CGForegroundInputPoster.swift'; then
  printf '%s\n' 'Global input outside the foreground emitter.' >&2
  exit 1
fi

swift build --package-path "$package_root" --product AstraVirtualCursorSidecar
sidecar_binary="$(swift build --package-path "$package_root" --show-bin-path)/AstraVirtualCursorSidecar"

python3 - "$sidecar_binary" <<'PY'
import json
import socket
import subprocess
import sys

sidecar_binary = sys.argv[1]
parent, child = socket.socketpair()
parent.settimeout(2)
process = subprocess.Popen(
    [sidecar_binary],
    stdin=child,
    stdout=child,
    stderr=subprocess.DEVNULL,
    close_fds=True,
)
child.close()
try:
    frame = bytearray()
    while not frame.endswith(b"\n"):
        chunk = parent.recv(513 - len(frame))
        if not chunk:
            raise AssertionError("cursor sidecar closed before the ready frame")
        frame.extend(chunk)
        if len(frame) > 512:
            raise AssertionError("cursor sidecar ready frame exceeded 512 bytes")

    hello = json.loads(frame)
    expected_panel = {
        "borderless_panel": True,
        "transparent": True,
        "nonactivating_panel": True,
        "ignores_mouse_events": True,
        "can_become_key": False,
        "can_become_main": False,
    }
    if set(hello) != {"type", "window_id", "panel"}:
        raise AssertionError(f"unexpected cursor sidecar hello fields: {hello!r}")
    if hello["type"] != "ready":
        raise AssertionError(f"unexpected cursor sidecar hello type: {hello!r}")
    if not isinstance(hello["window_id"], int) or hello["window_id"] <= 0:
        raise AssertionError(f"unsafe cursor sidecar window ID: {hello!r}")
    if hello["panel"] != expected_panel:
        raise AssertionError(f"unsafe cursor sidecar panel properties: {hello!r}")
finally:
    parent.close()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
        raise AssertionError("cursor sidecar did not exit after parent EOF")

print("AstraVirtualCursorSidecar: real ready/EOF smoke passed")
PY

swift_test_output="$(swift test --package-path "$package_root" --no-parallel 2>&1)"
printf '%s\n' "$swift_test_output"
swift_test_count="$(printf '%s\n' "$swift_test_output" | sed -nE 's/.*Test run with ([1-9][0-9]*) tests.*passed.*/\1/p')"
if [[ -z "$swift_test_count" ]]; then
  echo "Swift test gate did not report a nonzero test count" >&2
  exit 1
fi

harness_output="$(ASTRA_MACOS_COMPUTER_HELPER_HARNESS=1 \
  swift run --package-path "$package_root" AstraMacComputerHelperHarness 2>&1)"
printf '%s\n' "$harness_output"
harness_count="$(printf '%s\n' "$harness_output" | sed -nE 's/^AstraMacComputerHelperHarness: ([1-9][0-9]*) tests passed$/\1/p')"
if [[ -z "$harness_count" ]]; then
  echo "Native production harness did not report a nonzero test count" >&2
  exit 1
fi
