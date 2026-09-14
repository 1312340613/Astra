import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadTimelineDisplay, saveTimelineDisplay } from "./tui-settings.js";

const root = mkdtempSync(join(tmpdir(), "astra-timeline-settings-"));
const path = join(root, ".astra", "tui-settings.json");
process.env.AGENT_PROJECT_ROOT = root;

assert.equal(loadTimelineDisplay(), true);

const previousHome = process.env.ASTRA_HOME;
const profile = mkdtempSync(join(tmpdir(), "astra-private-profile-"));
process.env.ASTRA_HOME = profile;
saveTimelineDisplay(false);
assert.equal(loadTimelineDisplay(), false);
assert.deepEqual(JSON.parse(readFileSync(join(profile, "tui-settings.json"), "utf-8")), { timeline: false });
if (previousHome === undefined) delete process.env.ASTRA_HOME;
else process.env.ASTRA_HOME = previousHome;
saveTimelineDisplay(false);
assert.equal(loadTimelineDisplay(), false);
assert.deepEqual(JSON.parse(readFileSync(path, "utf-8")), { timeline: false });

writeFileSync(path, JSON.stringify({ theme: "glitchcity", timeline: "off", custom: 7 }));
assert.equal(loadTimelineDisplay(), true);
saveTimelineDisplay(true);
assert.deepEqual(JSON.parse(readFileSync(path, "utf-8")), {
  theme: "glitchcity",
  timeline: true,
  custom: 7,
});

writeFileSync(path, "not-json", "utf-8");
assert.equal(loadTimelineDisplay(), true);
