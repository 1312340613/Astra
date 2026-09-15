import test from "node:test";
import assert from "node:assert/strict";
import { ControlledBackendRestart, RESTART_EXIT_CODE } from "./controlled-restart.js";

const ready = { request_id: "a".repeat(32), session: "work_session" };

test("respawn requires a current ready receipt and reserved exit", () => {
  const restart = new ControlledBackendRestart();
  assert.equal(restart.onExit(RESTART_EXIT_CODE), null);
  assert.equal(restart.acceptReady({ ...ready, replayed: true }), false);
  assert.equal(restart.acceptReady(ready), true);
  assert.equal(restart.onExit(1), null);
  assert.equal(restart.acceptReady(ready), true);
  assert.equal(restart.onExit(RESTART_EXIT_CODE), ready.session);
  assert.equal(restart.restored("other"), false);
  assert.equal(restart.restored(ready.session), true);
  assert.equal(restart.restored(ready.session), false);
  assert.equal(restart.onExit(RESTART_EXIT_CODE), null);
});

test("cancelled or malformed requests never trigger respawn", () => {
  const restart = new ControlledBackendRestart();
  assert.equal(restart.acceptReady({ ...ready, session: "../elsewhere" }), false);
  assert.equal(restart.acceptReady(ready), true);
  restart.cancel();
  assert.equal(restart.onExit(RESTART_EXIT_CODE), null);
});
