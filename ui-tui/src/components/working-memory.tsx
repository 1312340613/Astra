import React from "react";
import { Box, Text } from "ink";
import type { WorkingMemory, WorkingStepStatus } from "../types.js";
import { useTheme } from "../theme-context.js";

export function workingProgress(memory: WorkingMemory): { completed: number; total: number } {
  const steps = memory.steps ?? [];
  return {
    completed: steps.filter((step) => step.status === "completed").length,
    total: steps.length,
  };
}

function displayWidth(value: string): number {
  return [...value].reduce((width, char) => width + (char.charCodeAt(0) > 255 ? 2 : 1), 0);
}

function shorten(value: string, maxWidth: number): string {
  if (displayWidth(value) <= maxWidth) return value;
  let output = "";
  let width = 0;
  for (const char of value) {
    const next = width + (char.charCodeAt(0) > 255 ? 2 : 1);
    if (next > Math.max(1, maxWidth - 1)) break;
    output += char;
    width = next;
  }
  return `${output}…`;
}

export function WorkingMemoryPanel({ memory, columns = 100, embedded = false }: { memory: WorkingMemory; columns?: number; embedded?: boolean }) {
  const theme = useTheme();
  const chrome = theme.chrome;
  const stepStyle: Record<WorkingStepStatus, { icon: string; color: string }> = {
    completed: { icon: "✓", color: theme.success },
    in_progress: { icon: "◉", color: theme.warning },
    pending: { icon: "○", color: theme.muted },
  };
  const steps = memory.steps ?? [];
  if (!memory.goal && !memory.progress && steps.length === 0) return null;
  const { completed, total } = workingProgress(memory);
  const textWidth = Math.max(20, columns - 10);

  const content = (
    <>
      <Box>
        <Text bold={theme.prefixBold} color={theme.accent}>{chrome?.memoryTitle ?? "工作计划"}</Text>
        {total > 0 && <Text color={completed === total ? theme.success : theme.warning}>  {completed}/{total}</Text>}
      </Box>
      {memory.goal && (
        <Text color={theme.text}><Text color={theme.header}>{chrome?.memoryGoalLabel ?? "目标  "}</Text>{shorten(memory.goal, textWidth)}</Text>
      )}
      {steps.map((step, index) => {
        const style = stepStyle[step.status];
        return (
          <Text key={`${index}-${step.text}`} color={style.color} dimColor={step.status === "pending"}>
            {style.icon} {index + 1}. {shorten(step.text, textWidth - 6)}
          </Text>
        );
      })}
      {memory.progress && (
        <Text color={theme.muted}><Text color={theme.accentAlt}>{chrome?.memoryProgressLabel ?? "进度  "}</Text>{shorten(memory.progress, textWidth)}</Text>
      )}
    </>
  );
  if (embedded) return <Box flexDirection="column" paddingX={1}>{content}</Box>;
  return (
    <Box flexDirection="column" borderStyle={chrome?.frameStyle ?? "round"} borderColor={theme.border} paddingX={1}>
      {content}
    </Box>
  );
}
