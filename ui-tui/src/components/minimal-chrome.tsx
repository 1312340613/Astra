import React from "react";
import { Box, Text } from "ink";
import { formatToolElapsed, formatToolName, type ActiveTool } from "./activity-dock.js";
import { useTheme } from "../theme-context.js";

function shorten(value: string, max: number): string {
  if (value.length <= max) return value;
  if (max <= 1) return "…";
  return `${value.slice(0, max - 1)}…`;
}

export function minimalModeStatus({
  busy,
  disconnected,
  toolCount,
}: {
  busy: boolean;
  disconnected: boolean;
  toolCount: number;
}): string {
  if (disconnected) return "SIGNAL LOST";
  if (busy || toolCount > 0) return "RUNNING";
  return "MINIMAL OPEN";
}

export function MinimalStatusDock({
  session = "",
  busy,
  disconnected,
  tools,
  now,
  columns,
  cacheHitTokens = 0,
  cacheMissTokens = 0,
  contextUsed = 0,
  contextLimit = 0,
  contextPct = 0,
  generationLabel,
}: {
  session?: string;
  busy: boolean;
  disconnected: boolean;
  tools: ActiveTool[];
  now: number;
  columns: number;
  cacheHitTokens?: number;
  cacheMissTokens?: number;
  contextUsed?: number;
  contextLimit?: number;
  contextPct?: number;
  generationLabel?: string;
}) {
  const theme = useTheme();
  const state = !disconnected && !tools.length && generationLabel
    ? generationLabel : minimalModeStatus({ busy, disconnected, toolCount: tools.length });
  const stateColor = disconnected ? theme.danger : busy || tools.length ? theme.warning : theme.success;
  const active = tools[0];
  const activity = active
    ? `${formatToolName(active.name)} · ${formatToolElapsed(active.startedAt, now)}${active.progress?.stage ? ` · ${active.progress.stage.replaceAll("_", " ").toUpperCase()}` : ""}`
    : "";
  const cacheInput = cacheHitTokens + cacheMissTokens;
  const cacheLabel = cacheInput > 0
    ? `cache ${Math.round(cacheHitTokens / cacheInput * 100)}%`
    : "";
  const ctxLabel = contextLimit
    ? `ctx ${contextUsed.toLocaleString()}/${contextLimit.toLocaleString()} ${contextPct.toFixed(0)}%`
    : "";
  const extraBudget = (cacheLabel ? 10 : 0) + (ctxLabel ? 16 : 0);
  const sessionLabel = shorten(
    session || "UNNAMED",
    Math.max(8, Math.min(28, columns - 44 - extraBudget)),
  );

  return (
    <Box overflow="hidden" borderStyle="single" borderColor={stateColor} paddingX={1}>
      <Text wrap="truncate-end">
        <Text bold color={stateColor}>{disconnected ? "×" : active ? "◆" : "○"} {state}</Text>
        <Text color={theme.subtle}> │ </Text>
        <Text color={theme.accent}>{sessionLabel}</Text>
        {activity && <>
          <Text color={theme.subtle}> │ </Text>
          <Text color={theme.warning}>{activity}</Text>
        </>}
        {cacheLabel && <>
          <Text color={theme.subtle}> │ </Text>
          <Text color={theme.success}>{cacheLabel}</Text>
        </>}
        {ctxLabel && <>
          <Text color={theme.subtle}> │ </Text>
          <Text color={theme.muted}>{ctxLabel}</Text>
        </>}
        <Text color={theme.subtle}> │ </Text>
        <Text color={theme.muted}>/minimal leave</Text>
      </Text>
    </Box>
  );
}
