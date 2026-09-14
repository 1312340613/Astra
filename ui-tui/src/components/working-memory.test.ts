import assert from "node:assert/strict";

import { workingProgress } from "./working-memory.js";

assert.deepEqual(workingProgress({}), { completed: 0, total: 0 });
assert.deepEqual(workingProgress({
  steps: [
    { text: "Inspect", status: "completed" },
    { text: "Implement", status: "in_progress" },
    { text: "Verify", status: "pending" },
  ],
}), { completed: 1, total: 3 });
