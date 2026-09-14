import type { AppshotClient } from "./appshot-client.js";
export async function runAppshotCommand(
  text: string,
  client?: Pick<AppshotClient, "command" | "state">,
): Promise<string | null> {
  if (!/^\/appshot(?:\s|$)/i.test(text.trim())) return null;
  const match = text
    .trim()
    .match(/^\/appshot\s+(status|enable|disable|shortcut)(?:\s+(\S+))?$/i);
  if (
    !match ||
    (match[1].toLowerCase() === "shortcut") !== !!match[2] ||
    (match[2]?.length ?? 0) > 128
  )
    return "Usage: /appshot status · shortcut <chord> · enable · disable";
  if (!client) return "Appshot: broker_unavailable";
  const name = match[1].toLowerCase() as
    "status" | "enable" | "disable" | "shortcut";
  try {
    const result = await client.command(name, match[2] ?? "");
    if (!result.ok) return `Appshot: ${result.code}`;
    if (name !== "status") {
      const refreshed = await client.command("status", "");
      if (!refreshed.ok) return "Appshot: broker_unavailable";
    }
    const s = client.state;
    return `Appshot: ${s.enabled ? "enabled" : "disabled"} · ${s.chord || "unavailable"} · ${s.registration} · ${s.connectedTuis} TUI · permission ${s.permission}`;
  } catch {
    return "Appshot: broker_unavailable";
  }
}
