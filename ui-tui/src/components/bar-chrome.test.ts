import assert from "node:assert/strict";
import {
  formatBarAmbiance,
  formatBarClock,
  formatBarEnvironment,
  formatLyraGlass,
  isQuietBarAction,
} from "./bar-chrome.js";

assert.match(formatBarClock(new Date(2026, 6, 17, 23, 7).getTime()), /^23:07$/);
assert.equal(isQuietBarAction("/sip", "bar"), true);
assert.equal(isQuietBarAction("/bar sip", "bar"), true);
assert.equal(isQuietBarAction("/sip", "work"), false);
assert.equal(isQuietBarAction("I sip the drink", "bar"), false);
assert.equal(formatBarAmbiance(
  { weather: "downpour", power: "flicker", music: "old_radio", radio: "local_news" },
  { turn_count: 6, phase: "deep" },
), "DEEP NIGHT // DOWNPOUR · FLICKER · OLD RADIO · LOCAL NEWS");
assert.equal(formatBarEnvironment(
  { weather: "rain", power: "stable", music: "low_synth", radio: "static" },
), "RAIN · STABLE · LOW SYNTH · STATIC");
assert.equal(formatLyraGlass({ active: false, name: "", note: "", fill: 0 }), "LYRA [---] WATER WAITING");
assert.equal(formatLyraGlass({ active: true, name: "夜班清水", note: "柠檬", fill: 2 }), "LYRA [##.] 夜班清水");
