#!/usr/bin/env bash
# Update the already-installed LaunchAgent at its stable path. Ad-hoc signing
# still binds the designated requirement to cdhash, so changed builds may need
# the user to renew the existing TCC grants even with a fixed identifier.
set -euo pipefail
[[ "$(uname -s)" == Darwin ]] || { echo 'Recorder deployment requires macOS.' >&2; exit 1; }
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
package_root="$script_dir/../native/macos-computer-helper"
destination="$HOME/Library/Application Support/Astra/bin/AstraActivityRecorder"
plist="$HOME/Library/LaunchAgents/com.astra.activity-recorder.plist"
identifier=com.astra.activity-recorder
# Validate before building or replacing anything. resolve() also rejects links
# that would redirect the stable executable into a build directory.
log_path="$(python3 - "$destination" "$plist" "$identifier" <<'PY'
import pathlib, plistlib, sys
path, plist, identifier = sys.argv[1:]
p = pathlib.Path(path)
if '.build' in p.resolve().parts or p.is_symlink():
    sys.exit('Refusing deployment through .build or a symlinked executable.')
with open(plist, 'rb') as f:
    agent = plistlib.load(f)
if agent.get('Label') != identifier or agent.get('ProgramArguments', [None])[0] != path or agent.get('Program', path) != path:
    sys.exit('LaunchAgent must launch the stable Application Support executable, outside .build.')
log = agent.get('StandardErrorPath')
if not log:
    sys.exit('LaunchAgent must specify StandardErrorPath for fresh accessibility verification.')
print(log)
PY
)"
service="gui/$(id -u)/$identifier"
verify_service_path() {
  python3 - "$destination" "$1" <<'PYVERIFY'
import sys
programs = [line.strip().removeprefix('program = ') for line in sys.argv[2].splitlines() if line.strip().startswith('program = ')]
if programs != [sys.argv[1]]:
    sys.exit('Loaded service must use the exact stable executable path.')
PYVERIFY
}
verify_service_path "$(launchctl print "$service")"
swift build -c release --package-path "$package_root" --product AstraActivityRecorder
binary_directory="$(swift build -c release --package-path "$package_root" --show-bin-path)"
source_binary="$binary_directory/AstraActivityRecorder"
[[ -f "$source_binary" ]] || { echo "Missing recorder build: $source_binary" >&2; exit 1; }
umask 077
mkdir -p "$(dirname -- "$destination")"
staged="$(mktemp "${destination}.XXXXXX")"
trap 'rm -f -- "$staged"' EXIT
cp "$source_binary" "$staged"
chmod 755 "$staged"
codesign --force --sign - --identifier "$identifier" "$staged"
codesign --verify --strict "$staged"
signature="$(codesign --display --verbose=4 "$staged" 2>&1)"
printf '%s\n' "$signature"
if ! printf '%s\n' "$signature" | grep -qx "Identifier=$identifier"; then
  echo 'Signed identifier mismatch; existing recorder preserved.' >&2
  exit 1
fi
log_state="$(python3 - "$log_path" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
if path.exists():
    stat = path.stat()
    print(json.dumps([stat.st_dev, stat.st_ino, stat.st_size]))
else:
    print('null')
PY
)"
mv -f "$staged" "$destination"
launchctl kickstart -k "$service"
service_state="$(launchctl print "$service")"
printf '%s\n' "$service_state" | grep -E '^[[:space:]]*(program|state|pid) ='
verify_service_path "$service_state"
python3 - "$log_path" "$log_state" <<'PY'
import json, pathlib, sys, time
path = pathlib.Path(sys.argv[1])
previous = json.loads(sys.argv[2])
offset = previous[2] if previous else 0
for _ in range(20):
    if path.exists():
        with path.open('rb') as f:
            import os
            stat = os.fstat(f.fileno())
            if previous and (stat.st_dev, stat.st_ino) != tuple(previous[:2]):
                sys.exit('Recorder deployed, but log changed identity; fresh evidence cannot be verified.')
            if stat.st_size < offset:
                sys.exit('Recorder deployed, but log was truncated; fresh evidence cannot be verified.')
            f.seek(offset)
            lines = f.read().decode('utf-8', errors='replace').splitlines()
        startup = [line for line in lines if 'recorder:' in line and 'accessibility=' in line]
        if startup:
            if 'accessibility=true' not in startup[-1]:
                print(startup[-1])
                sys.exit('Recorder reloaded but Accessibility is unavailable.')
            health = [line for line in lines if line.startswith('recorder: inputMonitoring=')]
            if health and all(flag in health[-1].split() for flag in (
                'inputMonitoring=true', 'tapInstalled=true', 'keyboardIncluded=true', 'tapEnabled=true'
            )):
                print(startup[-1])
                print(health[-1])
                sys.exit(0)
    time.sleep(0.5)
sys.exit('Recorder deployed, but fresh accessibility=true and healthy keyboard event tap evidence were not observed within 10s.')
PY
