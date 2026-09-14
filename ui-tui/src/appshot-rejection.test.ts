import test from "node:test";
import assert from "node:assert/strict";
import { appshotRejectionNotice } from "./appshot-rejection.js";
test("missing and invalid attachments give actionable recapture instructions", () => {
  for (const code of ["artifact_missing", "binding_mismatch", "invalid_ax", "artifact_hash"]) {
    const notice = appshotRejectionNotice(code);
    assert.ok(notice.includes(code));
    assert.ok(notice.includes("重新截图"));
    assert.ok(notice.includes("草稿已恢复"));
  }
});
test("transient and budget errors retain distinct recovery", () => {
  assert.match(appshotRejectionNotice("backend_busy"), /等任务结束/);
  assert.match(appshotRejectionNotice("context_budget_exceeded"), /压缩会话/);
});
test("unknown or hostile codes never reach terminal", () => {
  for (const value of ["\x1b[2Jsecret", "constructor", "__proto__", undefined]) {
    const notice = appshotRejectionNotice(value);
    assert.match(notice, /appshot_admission_failed/);
    assert.ok(!notice.includes("secret"));
    assert.ok(!notice.includes("\x1b"));
  }
});
