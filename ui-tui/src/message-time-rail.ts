export type TimeRailAnchor = {
  minuteKey: number;
  timestamp: number;
};

export type TimeRailLabelKind = "absolute" | "relative";

export type TimeRailDecision = {
  label: string;
  kind: TimeRailLabelKind;
  anchor: TimeRailAnchor;
};

export type TimeRailLayout = {
  width: number;
  prefixWidth: number;
};

export type TimeRail = TimeRailLayout & {
  label: string;
  kind: TimeRailLabelKind;
};

const LEADING_MESSAGE_TIME = /^<message_time>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})(?: 周[一二三四五六日])?<\/message_time>(?:\r?\n)?/u;
const CONVERSATION_ROLES = new Set(["user", "reasoning", "assistant", "tool"]);
const ROLE_MARKER = /^(.*?)(::|⇢|›|>|◇|†|⊙)$/u;

export function compactRailSeparator(separator: string): string {
  return separator.trim() || "│";
}

export function compactRolePrefix(prefix: string): string {
  const trimmed = prefix.trim();
  const match = trimmed.match(ROLE_MARKER);
  if (!match) return trimmed ? `${trimmed} ` : "";
  return `${match[1].trimEnd()}${match[2]} `;
}

export function decideTimeRail(
  timestamp: number | undefined,
  previousAnchor: TimeRailAnchor | null,
): TimeRailDecision | null {
  if (typeof timestamp !== "number" || !Number.isFinite(timestamp) || timestamp <= 0) return null;
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return null;
  const minuteKey = Math.floor(timestamp / 60_000);
  if (!previousAnchor || previousAnchor.minuteKey !== minuteKey) {
    const hours = String(date.getHours()).padStart(2, "0");
    const minutes = String(date.getMinutes()).padStart(2, "0");
    const anchor = { minuteKey, timestamp };
    return { label: `${hours}:${minutes}`, kind: "absolute", anchor };
  }

  const secondWithinMinute = timestamp < previousAnchor.timestamp
    ? 0
    : Math.min(59, Math.floor((timestamp - minuteKey * 60_000) / 1_000));
  return {
    label: `+${String(secondWithinMinute).padStart(2, "0")}s`,
    kind: "relative",
    anchor: previousAnchor,
  };
}

export function stripLegacyAssistantTimeMarker(content: string): string {
  return content.replace(LEADING_MESSAGE_TIME, "");
}

export class MessageTimeRailCursor {
  private anchor: TimeRailAnchor | null = null;

  next(
    role: string,
    timestamp: number | undefined,
    layout: TimeRailLayout,
  ): TimeRail | undefined {
    if (!CONVERSATION_ROLES.has(role)) return undefined;
    const decision = decideTimeRail(timestamp, this.anchor);
    if (!decision) return undefined;
    this.anchor = decision.anchor;
    return { label: decision.label, kind: decision.kind, ...layout };
  }

  reset(): void {
    this.anchor = null;
  }
}
