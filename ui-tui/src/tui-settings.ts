import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const settingsPath = () => resolve(
  process.env.ASTRA_HOME?.trim() || resolve(process.env.AGENT_PROJECT_ROOT || process.cwd(), ".astra"),
  "tui-settings.json",
);

export function readTuiSettings(): Record<string, unknown> {
  const path = settingsPath();
  if (!existsSync(path)) return {};
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, "utf-8"));
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

export function writeTuiSettings(patch: Record<string, unknown>): void {
  const path = settingsPath();
  mkdirSync(dirname(path), { recursive: true });
  const next = { ...readTuiSettings(), ...patch };
  writeFileSync(path, JSON.stringify(next, null, 2) + "\n", "utf-8");
}

export function loadTimelineDisplay(): boolean {
  const value = readTuiSettings().timeline;
  return typeof value === "boolean" ? value : true;
}

export function saveTimelineDisplay(enabled: boolean): void {
  writeTuiSettings({ timeline: enabled });
}
