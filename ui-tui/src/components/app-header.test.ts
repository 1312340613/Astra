import assert from "node:assert/strict";
import { responsiveMode } from "./app-header.js";

assert.equal(responsiveMode(50), "compact");
assert.equal(responsiveMode(69), "compact");
assert.equal(responsiveMode(70), "standard");
assert.equal(responsiveMode(110), "standard");
assert.equal(responsiveMode(111), "wide");
