import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { isBenignMacOSAllocatorDiagnostic, isBackendModelProgress } from "./backend-stderr.js";

const diagnostic =
  "python(74787) MallocStackLogging: can't turn off malloc stack logging because it was not enabled";

assert.equal(isBenignMacOSAllocatorDiagnostic(diagnostic, "darwin"), true);
assert.equal(isBenignMacOSAllocatorDiagnostic(`${diagnostic}.`, "darwin"), true);
assert.equal(isBenignMacOSAllocatorDiagnostic(diagnostic, "win32"), false);
assert.equal(isBenignMacOSAllocatorDiagnostic(diagnostic, "linux"), false);
assert.equal(
  isBenignMacOSAllocatorDiagnostic(`[backend] ${diagnostic}`, "darwin"),
  false,
);
assert.equal(
  isBenignMacOSAllocatorDiagnostic(`${diagnostic} unexpected suffix`, "darwin"),
  false,
);
assert.equal(
  isBenignMacOSAllocatorDiagnostic(
    "python(74787) malloc: pointer being freed was not allocated",
    "darwin",
  ),
  false,
);

const appSource = readFileSync(new URL("./app.tsx", import.meta.url), "utf8");
const stderrHandler = appSource.slice(
  appSource.indexOf('errRl.on("line"'),
  appSource.indexOf("proc.on(\"close\""),
);

assert.match(stderrHandler, /isBenignMacOSAllocatorDiagnostic\(line\)/);
assert.ok(
  stderrHandler.indexOf("isBenignMacOSAllocatorDiagnostic(line)") <
    stderrHandler.indexOf('addMessageRef.current("error"'),
);

for(const line of [
 "Fetching 9 files:   0%|          | 0/9 [00:00<?, ?it/s]",
 "Fetching 9 files: 100%|████████| 9/9 [00:00<00:00, 2948.20it/s]",
 "Loading checkpoint shards: 50%|████ | 1/2 [00:01<00:01, 1.00it/s]",
 "model.safetensors: 100%|████| 1.2G/1.2G [00:20<00:00, 60MB/s]",
])assert.equal(isBackendModelProgress(line),true,line);
for(const line of [
 "OSError: cannot download model", "Traceback (most recent call last):",
 "Fetching 9 files failed: ConnectionError", "ERROR: Fetching 9 files: 0%| | 0/9 [00:00<?, ?it/s]",
 "Fetching 9 files: 100%|████| 9/9 [00:00<00:00, 2it/s] ERROR: corrupt weights",
 "unexpected backend diagnostic",
])assert.equal(isBackendModelProgress(line),false,line);
