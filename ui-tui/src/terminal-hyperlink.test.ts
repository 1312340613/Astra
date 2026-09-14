import assert from "node:assert/strict";
import {
  formatTerminalHyperlink,
  resolveTerminalHyperlinkTarget,
  terminalHyperlinkDisplayText,
  terminalHyperlinkMode,
} from "./terminal-hyperlink.js";

assert.equal(resolveTerminalHyperlinkTarget("https://example.com/docs?q=1#top"), "https://example.com/docs?q=1#top");
assert.equal(resolveTerminalHyperlinkTarget("http://example.com"), "http://example.com");
assert.equal(resolveTerminalHyperlinkTarget("mailto:hello@example.com"), "mailto:hello@example.com");
assert.equal(
  resolveTerminalHyperlinkTarget("/Users/name/项目/a #?.ts:42:7"),
  "vscode://file/Users/name/%E9%A1%B9%E7%9B%AE/a%20%23%3F.ts:42:7",
);
assert.equal(
  resolveTerminalHyperlinkTarget("file:///Users/name/My%20File.ts"),
  "vscode://file/Users/name/My%20File.ts",
);
assert.equal(
  resolveTerminalHyperlinkTarget("file://localhost/Users/name/app.ts:9"),
  "vscode://file/Users/name/app.ts:9",
);
for (const target of [
  "C:\\Users\\name\\项目\\a #?.ts:42:7",
  "C:/Users/name/项目/a #?.ts:42:7",
  "file:///C:/Users/name/%E9%A1%B9%E7%9B%AE/a%20%23%3F.ts:42:7",
]) {
  assert.equal(
    resolveTerminalHyperlinkTarget(target),
    "vscode://file/C:/Users/name/%E9%A1%B9%E7%9B%AE/a%20%23%3F.ts:42:7",
  );
}
assert.equal(
  resolveTerminalHyperlinkTarget("/Users/name/../项目/a%20.ts:3"),
  "vscode://file/Users/%E9%A1%B9%E7%9B%AE/a%2520.ts:3",
);

for (const rejected of [
  undefined,
  "",
  "relative/file.ts:2",
  "javascript:alert",
  "data:text/plain,hello",
  "vscode://file/Users/name/app.ts:2",
  "file://server/share/app.ts",
  "file:///Users/name%2Fapp.ts",
  "file:///C:/Users/name%5Capp.ts",
  "file:///Users/name/%1Bapp.ts",
  "file:///Users/name/%FFapp.ts",
  "C:relative.ts",
  "C:\\Users\\name\\app.ts:0",
  "\\\\server\\share\\app.ts",
  "/Users/name/app.ts:0",
  "/Users/name/app.ts:-1",
  "/Users/name/app.ts:2:0",
  "https://example.com/\x1b]8;;https://evil.test",
  `https://example.com/${"x".repeat(4096)}`,
]) {
  assert.equal(resolveTerminalHyperlinkTarget(rejected), null, String(rejected));
}

assert.equal(
  formatTerminalHyperlink("文档", "https://example.com/docs", "osc8"),
  "\x1b]8;;https://example.com/docs\x07文档\x1b]8;;\x07",
);
assert.equal(formatTerminalHyperlink("危险", "javascript:alert"), "危险");
assert.equal(formatTerminalHyperlink("缺失", undefined), "缺失");

const adjacent = [
  formatTerminalHyperlink("A", "https://a.test", "osc8"),
  formatTerminalHyperlink("B", "https://b.test", "osc8"),
].join("");
assert.equal((adjacent.match(/\x1b\]8;;\x07/gu) ?? []).length, 2);
assert.equal(adjacent.endsWith("\x1b]8;;\x07"), true);

assert.equal(terminalHyperlinkMode({ TERM_PROGRAM: "Apple_Terminal" }), "visible-target");
assert.equal(terminalHyperlinkMode({ TERM_PROGRAM: "iTerm.app" }), "osc8");
assert.equal(terminalHyperlinkMode({}), "osc8");
assert.equal(
  terminalHyperlinkDisplayText("网页", "https://example.com/docs", "visible-target"),
  "网页 <https://example.com/docs>",
);
assert.equal(
  terminalHyperlinkDisplayText("源码", "/Users/name/项目/app.ts:42:7", "visible-target"),
  "源码 <vscode://file/Users/name/%E9%A1%B9%E7%9B%AE/app.ts:42:7>",
);
assert.equal(terminalHyperlinkDisplayText("危险", "javascript:alert", "visible-target"), "危险");
assert.equal(
  formatTerminalHyperlink("网页 <https://example.com/docs>", "https://example.com/docs", "visible-target"),
  "网页 <https://example.com/docs>",
);
