import assert from "node:assert/strict";
import {
  MessageTimeRailCursor,
  compactRailSeparator,
  compactRolePrefix,
  decideTimeRail,
  stripLegacyAssistantTimeMarker,
} from "./message-time-rail.js";

const anchor = new Date(2026, 7, 25, 9, 44, 10, 100).getTime();
const plusZero = anchor + 700;
const plusFour = anchor + 4_900;
const plusEleven = anchor + 11_200;
const nextMinute = new Date(2026, 7, 25, 9, 45, 0, 0).getTime();
const compactLayout = { width: 7, prefixWidth: 7 };
const legacyLayout = { width: 9, prefixWidth: 7 };

const firstDecision = decideTimeRail(anchor, null);
assert.equal(firstDecision?.kind, "absolute");
assert.equal(decideTimeRail(Number.NaN, firstDecision!.anchor), null);
assert.equal(decideTimeRail(anchor - 2_000, firstDecision!.anchor)?.label, "+00s");

const minuteScaleFirst = new Date(2026, 7, 25, 19, 52, 10, 0).getTime();
const minuteScaleLater = new Date(2026, 7, 25, 19, 52, 30, 0).getTime();
const minuteScaleAnchor = decideTimeRail(minuteScaleFirst, null)!.anchor;
assert.equal(decideTimeRail(minuteScaleLater, minuteScaleAnchor)?.label, "+30s");

const marker = "<message_time>2026-08-24T20:43:36+08:00</message_time>";
const weekdayMarker = "<message_time>2026-08-24T20:43:36+08:00 周一</message_time>";
assert.equal(stripLegacyAssistantTimeMarker(`${weekdayMarker}\nanswer`), "answer");
assert.equal(stripLegacyAssistantTimeMarker(`${weekdayMarker}\r\nanswer`), "answer");
assert.equal(stripLegacyAssistantTimeMarker(`answer ${weekdayMarker}`), `answer ${weekdayMarker}`);
const malformedWeekday = weekdayMarker.replace("周一", "周八");
assert.equal(stripLegacyAssistantTimeMarker(`${malformedWeekday}\nanswer`), `${malformedWeekday}\nanswer`);
assert.equal(stripLegacyAssistantTimeMarker(`${marker}\nanswer`), "answer");
assert.equal(stripLegacyAssistantTimeMarker(`${marker}\r\nanswer`), "answer");
assert.equal(stripLegacyAssistantTimeMarker(`answer ${marker}`), `answer ${marker}`);
assert.equal(
  stripLegacyAssistantTimeMarker("<message_time>example</message_time>\nanswer"),
  "<message_time>example</message_time>\nanswer",
);

assert.equal(compactRailSeparator(" │ "), "│");
assert.equal(compactRailSeparator(" :: "), "::");
assert.equal(compactRailSeparator("   "), "│");
assert.equal(compactRolePrefix("YOU  › "), "YOU› ");
assert.equal(compactRolePrefix("LYRA › "), "LYRA› ");
assert.equal(compactRolePrefix("CRT :: "), "CRT:: ");
assert.equal(compactRolePrefix("assistant "), "assistant ");

const conversationCursor = new MessageTimeRailCursor();
assert.deepEqual(conversationCursor.next("assistant", anchor, compactLayout), {
  label: "09:44",
  kind: "absolute",
  ...compactLayout,
});
assert.deepEqual(conversationCursor.next("tool", plusZero, compactLayout), {
  label: "+10s",
  kind: "relative",
  ...compactLayout,
});
assert.equal(conversationCursor.next("reasoning", plusFour, compactLayout)?.label, "+15s");
assert.equal(conversationCursor.next("assistant", plusEleven, compactLayout)?.label, "+21s");
assert.deepEqual(conversationCursor.next("user", nextMinute, compactLayout), {
  label: "09:45",
  kind: "absolute",
  ...compactLayout,
});
conversationCursor.reset();
assert.equal(conversationCursor.next("assistant", plusEleven, compactLayout)?.label, "09:44");

const cursor = new MessageTimeRailCursor();
const userRail = cursor.next("user", anchor, legacyLayout);
assert.deepEqual(userRail, { label: "09:44", kind: "absolute", ...legacyLayout });

assert.equal(cursor.next("user", plusEleven, legacyLayout)?.label, "+21s");

cursor.reset();
assert.equal(cursor.next("assistant", plusEleven, legacyLayout)?.label, "09:44");
assert.equal(cursor.next("tool", nextMinute, legacyLayout)?.label, "09:45");
