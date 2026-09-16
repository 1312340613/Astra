import assert from "node:assert/strict";
import { startsConversationCommand } from "./conversation-commands.js";

for (const command of ["/learn review", "/learn REVIEW Example", "/skills create", "/skills create x y", "/memory review old paths", "/doctor", "/doctor computer", "/diagnostics tasks", "/handoff Windows notes", "/conclave Compare two approaches"]) {
  assert.equal(startsConversationCommand(command), true, command);
}
for (const command of ["/learn history", "/learn migrate", "/skills show x", "/skills create --template x y", "/memory inspect", "/doctor --raw computer", "/diagnostics --raw", "/diagnostics json", "/handoff --raw notes", "/conclave", "/conclave config sources 2", "/mode high", "/cancel"]) {
  assert.equal(startsConversationCommand(command), false, command);
}
