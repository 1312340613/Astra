import assert from "node:assert/strict";
import {
  hasAmbiguousTerminalWidth,
  terminalSafeCut,
  terminalTextFragments,
  terminalTokens,
} from "./terminal-text.js";

assert.equal(hasAmbiguousTerminalWidth("क्‍ष"), true);
assert.equal(hasAmbiguousTerminalWidth("👩‍👩‍👧‍👦"), false);
assert.equal(hasAmbiguousTerminalWidth("🏳️‍🌈"), false);
assert.equal(hasAmbiguousTerminalWidth("👩‍⚕️"), false);
assert.equal(hasAmbiguousTerminalWidth("👨‍❤️‍👨"), false);
assert.equal(hasAmbiguousTerminalWidth("🇸🇬"), false);
assert.equal(hasAmbiguousTerminalWidth("e\u0301"), false);
assert.equal(hasAmbiguousTerminalWidth("中文 plain ASCII"), false);

const maxChars = 16;
const fixtures = [
  "\x1b[31mX",
  "\x1b]0;title\x07X",
  "🚀",
  "👩‍👩‍👧‍👦",
  "e\u0301",
  "🇸🇬",
  "क्‍ष",
];

const splitFixtures = [
  { token: "\x1b[31mX", splitAt: 1 },
  { token: "\x1b]0;title\x1b\\X", splitAt: 4 },
  { token: "🚀", splitAt: 1 },
  { token: "👩‍👩‍👧‍👦", splitAt: 2 },
  { token: "e\u0301", splitAt: 1 },
  { token: "🇸🇬", splitAt: 2 },
  { token: "क्‍ष", splitAt: 2 },
];

for (const { token, splitAt } of splitFixtures) {
  const prefix = "a".repeat(maxChars - splitAt);
  const left = prefix + token.slice(0, splitAt);
  const continuation = token.slice(splitAt);
  assert.equal(left.length, maxChars);
  assert.equal(
    terminalSafeCut(left, maxChars, continuation),
    prefix.length,
    `${JSON.stringify(token)} carries its incomplete terminal token without joining two full buffers`,
  );
}

for (const fixture of fixtures) {
  const prefix = "a".repeat(maxChars - 1);
  const source = prefix + fixture;
  const cut = terminalSafeCut(source, maxChars);
  assert.ok(cut < maxChars, `${JSON.stringify(fixture)} moves wholly after the safe cut`);
  assert.equal(source.slice(0, cut) + source.slice(cut), source);
  const fragments = terminalTextFragments(source, maxChars);
  assert.equal(fragments.join(""), source);
  assert.equal(fragments.every((fragment) => fragment.length <= maxChars), true);
  assert.equal(fragments.filter((fragment) => fragment.includes(fixture)).length, 1);
}

const ascii = "a".repeat(maxChars * 2 + 3);
assert.deepEqual(
  terminalTextFragments(ascii, maxChars).map((fragment) => fragment.length),
  [maxChars, maxChars, 3],
  "plain ASCII retains full-size fragments",
);
assert.equal(terminalTokens(fixtures.join("")).map((token) => token.text).join(""), fixtures.join(""));

console.log("terminal text boundary tests passed");
