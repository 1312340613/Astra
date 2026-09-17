import type { ContextCompactionEvent } from "./types.js";

export const COMPACTION_LABEL = "正在压缩上下文…";

export function compactionNotice(event: ContextCompactionEvent): string | null {
  if (![event.messages_before, event.messages_after].every((count) => Number.isSafeInteger(count) && count >= 0)) return null;
  switch (event.status) {
    case "started": return COMPACTION_LABEL;
    case "completed": return `上下文压缩完成：${event.messages_before} → ${event.messages_after} 条消息。`;
    case "failed": return "上下文压缩未完成。";
    case "cancelled": return "上下文压缩已取消。";
    default: return null;
  }
}
