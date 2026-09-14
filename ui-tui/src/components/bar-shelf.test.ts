import assert from "node:assert/strict";
import stringWidth from "string-width";
import { barShelfLayout, drinkFillMeter, drinkTemperatureLabel } from "./bar-shelf.js";
import type { BarDrinkState } from "../types.js";

assert.equal(drinkFillMeter(3), "[###]");
assert.equal(drinkFillMeter(2), "[##.]");
assert.equal(drinkFillMeter(1), "[#..]");
assert.equal(drinkFillMeter(0), "[...]");
assert.equal(drinkFillMeter(99), "[###]");
assert.equal(drinkFillMeter(-5), "[...]");
assert.equal(drinkTemperatureLabel("hot"), "HOT");
assert.equal(drinkTemperatureLabel("cold"), "COLD");
assert.equal(drinkTemperatureLabel("room"), "ROOM TEMP");

const longDrink: BarDrinkState = {
  active: true,
  name: "系统过载但名称也故意写得非常非常长".repeat(2),
  note: "高浓度的龙舌兰混入深色苦艾酒，带着一丝冷冽的薄荷香气，入口像是一次强制重启".repeat(2),
  tone: "cyan",
  temperature: "cold",
  fill: 2,
};
const wide = barShelfLayout(longDrink, 100);
assert.equal(wide.compact, false);
assert.equal(wide.hint, "/sip to drink");
assert.equal(wide.prefix.endsWith("…"), true);
assert.equal(wide.note.endsWith("…"), true);
assert.equal(
  stringWidth(`BAR SHELF // ${wide.prefix}${wide.spacer}${wide.hint}`),
  95,
);
assert.ok(stringWidth(`TASTING // ${wide.note}`) <= 95);

const compact = barShelfLayout(longDrink, 60);
assert.equal(compact.compact, true);
assert.equal(compact.hint, "/sip");
assert.equal(compact.note, "");
assert.equal(
  stringWidth(`SHELF // ${compact.prefix}${compact.spacer}${compact.hint}`),
  55,
);
