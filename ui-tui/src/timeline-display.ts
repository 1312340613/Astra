export type TimelineCommandResult =
  | { ok: true; enabled: boolean }
  | { ok: false; error: string };

export function resolveTimelineCommand(
  input: string,
  current: boolean,
): TimelineCommandResult | null {
  const trimmed = input.trim();
  if (!/^\/timeline(?:\s|$)/i.test(trimmed)) return null;
  const match = trimmed.match(/^\/timeline(?:\s+(\S+))?$/i);
  if (!match) return { ok: false, error: "Usage: /timeline [on|off]" };

  const mode = match[1]?.toLowerCase();
  if (mode === undefined) return { ok: true, enabled: !current };
  if (mode === "on") return { ok: true, enabled: true };
  if (mode === "off") return { ok: true, enabled: false };
  return { ok: false, error: "Usage: /timeline [on|off]" };
}

export function shouldMessageUseTimeline(
  role: string,
  visible: boolean,
  explicitToolCompletion = false,
): boolean {
  if (!visible) return false;
  if (role === "tool") return explicitToolCompletion;
  return role === "user" || role === "reasoning" || role === "assistant";
}

export function shouldResetTimelineCursor(current: boolean, next: boolean): boolean {
  return !current && next;
}
