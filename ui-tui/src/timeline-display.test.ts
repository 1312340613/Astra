import assert from "node:assert/strict";
import {
  resolveTimelineCommand,
  shouldMessageUseTimeline,
  shouldResetTimelineCursor,
} from "./timeline-display.js";

assert.deepEqual(resolveTimelineCommand("/timeline", true), { ok: true, enabled: false });
assert.deepEqual(resolveTimelineCommand("/TIMELINE", false), { ok: true, enabled: true });
assert.deepEqual(resolveTimelineCommand("/timeline on", false), { ok: true, enabled: true });
assert.deepEqual(resolveTimelineCommand("/timeline off", true), { ok: true, enabled: false });
assert.deepEqual(resolveTimelineCommand("/timeline maybe", true), {
  ok: false,
  error: "Usage: /timeline [on|off]",
});
assert.equal(resolveTimelineCommand("/theme", true), null);
assert.equal(resolveTimelineCommand("/timelinefoo", true), null);

assert.equal(shouldMessageUseTimeline("user", true), true);
assert.equal(shouldMessageUseTimeline("reasoning", true), true);
assert.equal(shouldMessageUseTimeline("assistant", true), true);
assert.equal(shouldMessageUseTimeline("tool", true), false);
assert.equal(shouldMessageUseTimeline("tool", true, true), true);
assert.equal(shouldMessageUseTimeline("tool", false, true), false);
assert.equal(shouldMessageUseTimeline("system", true), false);

assert.equal(shouldResetTimelineCursor(false, true), true);
assert.equal(shouldResetTimelineCursor(true, true), false);
assert.equal(shouldResetTimelineCursor(true, false), false);
assert.equal(shouldResetTimelineCursor(false, false), false);
