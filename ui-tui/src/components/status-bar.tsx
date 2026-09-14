import React from "react";
import { Text, Box } from "ink";
import stringWidth from "string-width";
import { THEMES, type UiTheme } from "../theme.js";
import { useTheme } from "../theme-context.js";
import type { ComputerControl, ComputerStateEvent } from "../types.js";

interface Props {
  model: string;
  totalTokens: number;
  promptTokens: number;
  completionTokens: number;
  cacheHitTokens?: number;
  cacheMissTokens?: number;
  contextUsed?: number;
  contextPct: number;
  contextLimit?: number;
  status: string;
  scrollOffset?: number;
  maxScrollOffset?: number;
  showReasoning?: boolean;
  sessionName?: string;
  columns?: number;
  computer?: ComputerUiState;
}

type Segment = { text: string; color?: string };

const TERMINAL_ESCAPE = /(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[PX^_][\s\S]*?\x1b\\|\x1b\[[0-?]*[ -/]*[@-~]|[\x90\x9b\x9d\x9e\x9f][\s\S]*?\x9c)/g;
const TERMINAL_OR_BIDI_CONTROL = /[\u0000-\u001f\u007f-\u009f\u061c\u200b\u200c\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]/g;

export function sanitizeComputerApplication(value: string): string {
  return value
    .normalize("NFC")
    .replace(TERMINAL_ESCAPE, "")
    .replace(TERMINAL_OR_BIDI_CONTROL, (character) => /\s/.test(character) ? " " : "")
    .replace(/\s+/g, " ")
    .trim();
}

export type ComputerUiState = {
  active: boolean;
  handedOff: boolean;
  control?: ComputerControl;
  application?: string;
  permission?: "available" | "degraded" | "unsupported";
};

const COMPUTER_CONTROLS: readonly ComputerControl[] = [
  "inactive", "background", "foreground_takeover", "user_control", "paused",
];

function isComputerControl(value: unknown): value is ComputerControl {
  return typeof value === "string" && COMPUTER_CONTROLS.includes(value as ComputerControl);
}

export function normalizeComputerControl(state: ComputerUiState): ComputerControl {
  if (state.handedOff && state.active) return "user_control";
  if (isComputerControl(state.control)) {
    if (!state.active) return "inactive";
    return state.control === "inactive" ? "background" : state.control!;
  }
  return state.active ? "background" : "inactive";
}

export function mergeComputerStateEvent(
  current: ComputerUiState,
  event: ComputerStateEvent,
): ComputerUiState {
  const incoming: ComputerUiState = {
    active: event.active,
    handedOff: event.handed_off,
    control: event.control,
    application: event.application,
    permission: event.permission,
  };
  if (isComputerControl(event.control)) {
    return { ...incoming, control: normalizeComputerControl(incoming) };
  }
  if (["paused", "foreground_takeover", "user_control"].includes(normalizeComputerControl(current))) {
    return current;
  }
  return { ...incoming, control: normalizeComputerControl(incoming) };
}

export function computerIndicator(state?: ComputerUiState): { label: string; color: "active" | "handoff" | "degraded" | "unsupported" | "" } {
  if (!state) return { label: "", color: "" };
  const application = shorten(sanitizeComputerApplication(state.application ?? ""), 28);
  const control = normalizeComputerControl(state);
  if (control === "user_control") {
    return { label: `USER CONTROL${application ? ` · ${application}` : ""}`, color: "handoff" };
  }
  if (control === "paused") return { label: "COMPUTER PAUSED", color: "handoff" };
  if (control === "foreground_takeover") {
    return { label: `TAKEOVER${application ? ` · ${application}` : ""}`, color: "active" };
  }
  if (control === "background") {
    return { label: `COOP${application ? ` · ${application}` : ""}`, color: "active" };
  }
  if (state.permission === "degraded") return { label: "COMPUTER DEGRADED", color: "degraded" };
  if (state.permission === "unsupported") return { label: "COMPUTER UNSUPPORTED", color: "unsupported" };
  return { label: "", color: "" };
}

function fmtK(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  return `${Math.round(n / 100) / 10}K`;
}

export function truncateDisplayText(value: string, max: number): string {
  if (max <= 0) return "";
  if (stringWidth(value) <= max) return value;
  if (max <= 1) return "…";
  const segments = typeof Intl.Segmenter === "function"
    ? Array.from(new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(value), ({ segment }) => segment)
    : Array.from(value);
  let output = "";
  for (const segment of segments) {
    if (stringWidth(output) + stringWidth(segment) > max - 1) break;
    output += segment;
  }
  return `${output}…`;
}

function shorten(value: string, max: number): string {
  return truncateDisplayText(value, max);
}

function ctxColor(pct: number, theme: UiTheme): string {
  if (pct > 85) return theme.danger;
  if (pct > 60) return theme.warning;
  return theme.success;
}

function totalLength(segments: Segment[]): number {
  return segments.reduce((n, s) => n + stringWidth(s.text), 0);
}

export function buildStatusParts({
  columns,
  model,
  variant = "full",
  sessionName = "",
  contextUsed,
  promptTokens,
  totalTokens,
  cacheHitTokens = 0,
  cacheMissTokens = 0,
  contextPct,
  contextLimit,
  showReasoning = false,
  scrollText = "",
  computer,
  theme = THEMES.hermes,
}: {
  columns: number;
  model: string;
  variant?: "full" | "details";
  sessionName?: string;
  contextUsed?: number;
  promptTokens: number;
  totalTokens: number;
  cacheHitTokens?: number;
  cacheMissTokens?: number;
  contextPct: number;
  contextLimit?: number;
  showReasoning?: boolean;
  scrollText?: string;
  computer?: ComputerUiState;
  theme?: UiTheme;
}): Segment[] {
  const innerWidth = Math.max(0, columns - 4);
  const sep: Segment = { text: theme.chrome?.separator ?? " · ", color: theme.subtle };

  const ctxUsed = contextUsed ?? promptTokens;
  const ctxStr = contextLimit
    ? `${fmtK(ctxUsed)}/${fmtK(contextLimit)}${variant === "full" ? ` ${contextPct.toFixed(0)}%` : ""}`
    : `${totalTokens.toLocaleString()}`;
  const cacheInput = cacheHitTokens + cacheMissTokens;
  const cacheStr = cacheInput > 0
    ? `${(cacheHitTokens / cacheInput * 100).toFixed(0)}%`
    : "";
  const computerLabel = computerIndicator(computer).label;

  function makeLine(modelLen: number, includeSession: boolean, includeScroll: boolean, includeComputer: boolean): Segment[] {
    const segments: Segment[] = [];
    const add = (segment: Segment) => {
      if (segments.length) segments.push(sep);
      segments.push(segment);
    };
    if (includeComputer && computerLabel) {
      add({ text: computerLabel, color: computer?.handedOff ? theme.warning : computer?.permission === "degraded" ? theme.danger : theme.accentAlt });
    }
    if (variant === "full") add({ text: shorten(model, modelLen), color: theme.accent });
    // session (optional)
    if (includeSession && sessionName) {
      const sessionBudget = Math.max(10, innerWidth - totalLength(segments) - 20);
      add({ text: `session ${shorten(sessionName, sessionBudget)}`, color: theme.muted });
    }
    // ctx
    add({ text: `ctx ${ctxStr}`, color: ctxColor(contextPct, theme) });
    if (cacheStr && columns >= 80) {
      add({ text: `cache ${cacheStr}`, color: theme.success });
    }
    // reasoning
    if (variant === "full") add({ text: showReasoning ? "R" : "r", color: theme.muted });
    // scroll
    if (includeScroll && scrollText) {
      add({ text: scrollText, color: theme.muted });
    }
    return segments;
  }

  // Try full-width layout
  let segments = makeLine(columns < 90 ? 14 : 24, columns >= 96, columns >= 90, true);
  if (totalLength(segments) <= innerWidth) return segments;

  // Remove scroll text
  segments = makeLine(columns < 90 ? 14 : 24, columns >= 96, false, true);
  if (totalLength(segments) <= innerWidth) return segments;

  // Shorten model
  segments = makeLine(12, false, false, true);
  if (totalLength(segments) <= innerWidth) return segments;

  // Ultra compact: preserve Computer Use state before lower-priority model details.
  if (computerLabel) {
    const context = `ctx ${contextPct.toFixed(0)}%`;
    const availableForLabel = innerWidth - stringWidth(sep.text) - stringWidth(context);
    if (availableForLabel > 0) {
      return [
        { text: shorten(computerLabel, availableForLabel), color: computer?.handedOff ? theme.warning : theme.accentAlt },
        sep,
        { text: context, color: ctxColor(contextPct, theme) },
      ];
    }
    if (stringWidth(context) <= innerWidth) {
      return [{ text: context, color: ctxColor(contextPct, theme) }];
    }
    return [
      { text: shorten(computerLabel, innerWidth), color: computer?.handedOff ? theme.warning : theme.accentAlt },
    ];
  }
  // Ultra compact: retain context if it fits, otherwise a grapheme-safe model.
  const context = `ctx ${contextPct.toFixed(0)}%`;
  if (variant === "details") return [{ text: shorten(`ctx ${ctxStr}`, innerWidth), color: ctxColor(contextPct, theme) }];
  const modelBudget = innerWidth - stringWidth(sep.text) - stringWidth(context);
  if (modelBudget > 0) {
    return [
      { text: shorten(model, modelBudget), color: theme.accent },
      sep,
      { text: context, color: ctxColor(contextPct, theme) },
    ];
  }
  if (stringWidth(context) <= innerWidth) {
    return [{ text: context, color: ctxColor(contextPct, theme) }];
  }
  return [{ text: shorten(model, innerWidth), color: theme.accent }];
}

export function StatusBar({
  model,
  totalTokens,
  promptTokens,
  completionTokens,
  cacheHitTokens = 0,
  cacheMissTokens = 0,
  contextUsed,
  contextPct,
  contextLimit,
  status,
  scrollOffset = 0,
  maxScrollOffset = 0,
  showReasoning = false,
  sessionName = "",
  columns = process.stdout.columns || 100,
  computer,
}: Props) {
  const theme = useTheme();
  const chrome = theme.chrome;
  const taskRunning = status.startsWith("task ");
  const reviewRunning = status.startsWith("review ");
  const statusIcon = status === "thinking" || taskRunning || reviewRunning ? "◉" : status === "disconnected" ? "✕" : "●";
  const statusColor =
    status === "thinking" || taskRunning || reviewRunning ? theme.warning : status === "disconnected" ? theme.danger : theme.success;

  const scrollText =
    maxScrollOffset > 0
      ? scrollOffset > 0
        ? `↑${scrollOffset}/${maxScrollOffset}`
        : "▼"
      : "";

  const segments = buildStatusParts({
    columns,
    model,
    sessionName,
    contextUsed,
    promptTokens,
    totalTokens,
    cacheHitTokens,
    cacheMissTokens,
    contextPct,
    contextLimit,
    showReasoning,
    scrollText,
    computer,
    theme,
  });

  return (
    <Box borderStyle={chrome?.frameStyle ?? "single"} borderColor={theme.border} paddingX={1} height={3} overflow="hidden">
      {chrome?.statusPrefix && (
        <>
          <Text bold color={theme.accent} backgroundColor={theme.statusBackground}>{chrome.statusPrefix}</Text>
          <Text color={theme.subtle} backgroundColor={theme.statusBackground}>{chrome.separator ?? " · "}</Text>
        </>
      )}
      <Text color={statusColor} backgroundColor={theme.statusBackground}>
        {statusIcon} {status}
      </Text>
      <Text color={theme.subtle} backgroundColor={theme.statusBackground}>{chrome?.separator ?? " · "}</Text>
      {segments.map((seg, i) => (
        <Text key={i} color={seg.color} backgroundColor={theme.statusBackground}>
          {seg.text}
        </Text>
      ))}
    </Box>
  );
}
