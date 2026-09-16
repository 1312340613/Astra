/** Busy/stream admission only. The backend owns parsing and workflow scope. */
export function startsConversationCommand(text: string): boolean {
  const trimmed = text.trim();
  if (/^\/(?:doctor|diagnostics|handoff)\s+--raw(?:\s|$)/i.test(trimmed)) return false;
  if (/^\/diagnostics\s+json(?:\s|$)/i.test(trimmed)) return false;
  if (/^\/skills\s+create\s+--template(?:\s|$)/i.test(trimmed)) return false;
  if (/^\/conclave\s+config(?:\s|$)/i.test(trimmed)) return false;
  return /^\/(?:doctor|diagnostics|handoff)(?:\s|$)/i.test(trimmed)
    || /^\/learn\s+review(?:\s|$)/i.test(trimmed)
    || /^\/skills\s+create(?:\s|$)/i.test(trimmed)
    || /^\/memory\s+review(?:\s|$)/i.test(trimmed)
    || /^\/conclave\s+\S/i.test(trimmed);
}
