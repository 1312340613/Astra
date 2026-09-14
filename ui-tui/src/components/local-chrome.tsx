import type { LocalModeDefinition } from "../types.js";
import React from "react";
import { Box, Text } from "ink";
import { useTheme } from "../theme-context.js";

function shorten(value: string, max: number): string {
  if (value.length <= max) return value;
  if (max <= 1) return "…";
  return `${value.slice(0, max - 1)}…`;
}

export function localModeStatus({
  busy,
  disconnected,
  definition,
}: {
  busy: boolean;
  disconnected: boolean;
  definition?: LocalModeDefinition | null;
}): string {
  if (disconnected) return "SIGNAL LOST";
  if (busy) return definition?.ui.busy ?? "WORKING…";
  return definition?.ui.idle ?? "LOCAL MODE";
}

export function LocalStatusDock({
  session = "",
  busy,
  disconnected,
  definition,
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
  definition?: LocalModeDefinition | null;
  columns: number;
  cacheHitTokens?: number;
  cacheMissTokens?: number;
  contextUsed?: number;
  contextLimit?: number;
  contextPct?: number;
  generationLabel?: string;
}) {
  const theme = useTheme();
  const state = !disconnected && generationLabel ? generationLabel : localModeStatus({ busy, disconnected, definition });
  const stateColor = disconnected ? theme.danger : busy ? theme.warning : theme.success;
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
        <Text bold color={stateColor}>{disconnected ? "×" : busy ? "◆" : "○"} {state}</Text>
        <Text color={theme.subtle}> │ </Text>
        <Text color={theme.accent}>{sessionLabel}</Text>
        {cacheLabel && <>
          <Text color={theme.subtle}> │ </Text>
          <Text color={theme.success}>{cacheLabel}</Text>
        </>}
        {ctxLabel && <>
          <Text color={theme.subtle}> │ </Text>
          <Text color={theme.muted}>{ctxLabel}</Text>
        </>}
        <Text color={theme.subtle}> │ </Text>
        <Text color={theme.muted}>{definition ? `${definition.command} leave` : ""}</Text>
      </Text>
    </Box>
  );
}
