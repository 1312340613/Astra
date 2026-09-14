import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  DEFAULT_THEME,
  loadThemeName,
  markLineSegments,
  saveThemeName,
  THEMES,
  THEME_NAMES,
} from "./theme.js";

assert.equal(DEFAULT_THEME, "hermes");
assert.deepEqual(THEME_NAMES, [
  "hermes",
  "glitchcity",
  "classic",
  "nord",
  "dracula",
  "solarized",
  "gruvbox",
  "lyra",
  "moonlit",
  "phosphor",
  "obsidian",
]);

assert.equal(THEMES.hermes.text, "#FFF8DC");
assert.equal(THEMES.hermes.accent, "#FFBF00");
assert.equal(THEMES.hermes.border, "#CD7F32");
assert.equal(THEMES.hermes.statusBackground, "#1A1A2E");

assert.equal(THEMES.glitchcity.accent, "#FF4FA3");
assert.equal(THEMES.glitchcity.accentAlt, "#42D9C8");
assert.equal(THEMES.glitchcity.header, "#FFB84D");
assert.equal(THEMES.glitchcity.chrome?.frameStyle, "double");
assert.equal(THEMES.glitchcity.chrome?.brand, "GLITCH CITY // AGENT BAR");
assert.equal(THEMES.glitchcity.chrome?.rolePrefixes?.assistant, "LYRA › ");
assert.equal(THEMES.glitchcity.console.bootTitle, "ASTRA // BOOT CONSOLE");

assert.equal(THEMES.lyra.accent, "#E5484D");
assert.equal(THEMES.lyra.chrome?.brand, "LYRA");
assert.equal(THEMES.lyra.chrome?.rolePrefixes?.assistant, "LYRA ⇢ ");
assert.equal(THEMES.lyra.console.bootTitle, "LYRA // NIGHT TERMINAL");
assert.equal(THEMES.lyra.console.markPalette?.["▒"], "accent");
assert.equal(THEMES.lyra.console.markPalette?.["█"], "muted");
const lyraEye = markLineSegments("███▒▒████", THEMES.lyra.console.markPalette, THEMES.lyra, THEMES.lyra.accent);
assert.equal(lyraEye.length, 3);
assert.equal(lyraEye[1]?.color, THEMES.lyra.accent);
assert.equal(markLineSegments("anything", undefined, THEMES.lyra, "fallback")[0]?.color, "fallback");

assert.equal(THEMES.moonlit.text, "#2E2A2C");
assert.equal(THEMES.moonlit.accent, "#C73E5A");
assert.equal(THEMES.moonlit.chrome?.frameStyle, "single");

assert.equal(THEMES.phosphor.accent, "#46F58C");
assert.equal(THEMES.phosphor.chrome?.brand, "PHOSPHOR // GREEN SCREEN");
assert.equal(THEMES.phosphor.console.bootTitle, "PHOSPHOR // WAVEFORM TERMINAL");

assert.equal(THEMES.obsidian.accent, "#4DA3FF");
assert.equal(THEMES.obsidian.chrome?.brand, "OBSIDIAN");
assert.equal(THEMES.obsidian.console.bootTitle, "OBSIDIAN // VOID BOOT");

assert.equal(THEMES.classic.code, "yellowBright");
assert.equal(THEMES.classic.codeBackground, "gray");
assert.equal(THEMES.classic.strongBold, true);
assert.equal(THEMES.classic.menuInverse, true);

assert.equal(new Set(THEME_NAMES.map((name) => THEMES[name].console.bootTitle)).size, THEME_NAMES.length);
assert.equal(new Set(THEME_NAMES.map((name) => THEMES[name].console.consoleTitle)).size, THEME_NAMES.length);
for (const name of THEME_NAMES) {
  assert.ok(THEMES[name].chrome?.brand, `${name} must define a complete header identity`);
  assert.ok(THEMES[name].chrome?.promptLabel, `${name} must define an input identity`);
  assert.ok(THEMES[name].chrome?.rolePrefixes?.assistant, `${name} must define a conversation identity`);
  assert.equal(THEMES[name].console.phases.length, 4, `${name} must define four boot phases`);
  assert.ok(THEMES[name].console.mark.length >= 5, `${name} must define a console mark`);
}

const projectRoot = mkdtempSync(join(tmpdir(), "agent-theme-"));
process.env.AGENT_PROJECT_ROOT = projectRoot;
assert.equal(loadThemeName(), "hermes");
saveThemeName("dracula");
assert.equal(loadThemeName(), "dracula");
assert.equal(
  JSON.parse(readFileSync(join(projectRoot, ".astra", "tui-settings.json"), "utf-8")).theme,
  "dracula",
);
saveThemeName("glitchcity");
assert.equal(loadThemeName(), "glitchcity");

const settingsPath = join(projectRoot, ".astra", "tui-settings.json");
const saved = JSON.parse(readFileSync(settingsPath, "utf-8"));
saved.timeline = false;
saved.custom = "keep";
writeFileSync(settingsPath, JSON.stringify(saved), "utf-8");
saveThemeName("nord");
assert.deepEqual(JSON.parse(readFileSync(settingsPath, "utf-8")), {
  theme: "nord",
  timeline: false,
  custom: "keep",
});
