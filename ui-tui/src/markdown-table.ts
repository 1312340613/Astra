import {
  hasAmbiguousTerminalWidth,
  terminalTokens,
  terminalWidth,
} from "./terminal-text.js";

export { terminalWidth } from "./terminal-text.js";

export type RenderedMarkdownTable = {
  consumed: number;
  mode: "grid" | "stacked";
  lines: string[];
};

export type StreamingMarkdownBlock = {
  kind: "text" | "table";
  lines: string[];
};

export type StreamingTableDecision = {
  pending: string[];
  blocks: StreamingMarkdownBlock[];
};

function visibleText(text: string): string {
  return text
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/(\*\*|__)(.*?)\1/g, "$2")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\\\|/g, "|")
    .trim();
}

export function parseMarkdownTableRow(line: string): string[] | null {
  const trimmed = line.trim();
  if (!trimmed.includes("|")) return null;
  const source = trimmed.replace(/^\|/, "").replace(/\|$/, "");
  const cells: string[] = [];
  let cell = "";
  let escaped = false;
  for (const ch of source) {
    if (escaped) {
      cell += ch === "|" ? "|" : `\\${ch}`;
      escaped = false;
    } else if (ch === "\\") {
      escaped = true;
    } else if (ch === "|") {
      cells.push(visibleText(cell));
      cell = "";
    } else {
      cell += ch;
    }
  }
  if (escaped) cell += "\\";
  cells.push(visibleText(cell));
  return cells;
}

export function compatibleMarkdownTableRow(line: string, columnCount: number): string[] | null {
  const cells = parseMarkdownTableRow(line);
  if (!cells || columnCount <= 0) return null;
  const trimmed = line.trim();
  const leadingPipe = trimmed.startsWith("|");
  if (cells.length > columnCount) {
    // Preserve only the recognized provider-truncation sentinel after the last
    // complete pipe. Any other suffix is a real cell in leading-only pipe syntax.
    if (!leadingPipe || trimmed.endsWith("|")) return null;
    const lastCompletePipe = trimmed.lastIndexOf("|");
    const suffix = trimmed.slice(lastCompletePipe + 1).trim();
    const completeCells = parseMarkdownTableRow(trimmed.slice(0, lastCompletePipe + 1));
    const providerTruncation = suffix === "..." || suffix === "…";
    return providerTruncation && completeCells?.length === columnCount ? completeCells : null;
  }
  if (cells.length < columnCount && !leadingPipe) return null;
  return [...cells, ...Array(columnCount - cells.length).fill("")];
}

export function isMarkdownTableStart(header: string, divider: string): boolean {
  const headers = parseMarkdownTableRow(header);
  const dividers = parseMarkdownTableRow(divider);
  return Boolean(headers && dividers && headers.length === dividers.length && isDivider(dividers));
}

export function consumeStreamingTableLine(pending: string[], line: string): StreamingTableDecision {
  if (pending.length === 0) {
    return line.includes("|")
      ? { pending: [line], blocks: [] }
      : { pending: [], blocks: [{ kind: "text", lines: [line] }] };
  }

  if (pending.length === 1) {
    if (isMarkdownTableStart(pending[0], line)) {
      return { pending: [...pending, line], blocks: [] };
    }
    const next = consumeStreamingTableLine([], line);
    return {
      pending: next.pending,
      blocks: [{ kind: "text", lines: pending }, ...next.blocks],
    };
  }

  const headerCells = parseMarkdownTableRow(pending[0]);
  if (headerCells && compatibleMarkdownTableRow(line, headerCells.length)) {
    return { pending: [...pending, line], blocks: [] };
  }

  const next = consumeStreamingTableLine([], line);
  return {
    pending: next.pending,
    blocks: [{ kind: "table", lines: pending }, ...next.blocks],
  };
}

function isDivider(cells: string[]): boolean {
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s/g, "")));
}

function sliceWidth(text: string, maxWidth: number): [string, string] {
  let width = 0;
  let tokenIndex = 0;
  const tokens = terminalTokens(text);
  let head = "";
  for (const token of tokens) {
    const next = width + token.width;
    if (next > maxWidth && head) break;
    width = next;
    head += token.text;
    tokenIndex += 1;
    if (width >= maxWidth) break;
  }
  return [head, tokens.slice(tokenIndex).map((token) => token.text).join("")];
}

function wrapCell(text: string, width: number): string[] {
  if (!text) return [""];
  const parts: string[] = [];
  let rest = text;
  while (rest) {
    const [head, tail] = sliceWidth(rest, width);
    parts.push(head);
    rest = tail;
  }
  return parts;
}

function pad(text: string, width: number, align: "left" | "center" | "right" = "left"): string {
  const missing = Math.max(0, width - terminalWidth(text));
  if (align === "right") return " ".repeat(missing) + text;
  if (align === "center") {
    const left = Math.floor(missing / 2);
    return " ".repeat(left) + text + " ".repeat(missing - left);
  }
  return text + " ".repeat(missing);
}

function renderStacked(headers: string[], rows: string[][], width: number): string[] {
  const output: string[] = [];
  rows.forEach((row, rowIndex) => {
    output.push(`┌─ Row ${rowIndex + 1}`);
    headers.forEach((header, colIndex) => {
      const label = `${header || `Column ${colIndex + 1}`}: `;
      const valueWidth = Math.max(8, width - 3 - terminalWidth(label));
      const wrapped = wrapCell(row[colIndex] ?? "", valueWidth);
      output.push(`│  ${label}${wrapped[0] ?? ""}`);
      for (const continuation of wrapped.slice(1)) {
        output.push(`│  ${" ".repeat(terminalWidth(label))}${continuation}`);
      }
    });
    output.push("└" + "─".repeat(Math.max(3, Math.min(width - 1, 20))));
  });
  return output;
}

export function renderMarkdownTable(source: string[], start: number, width: number): RenderedMarkdownTable | null {
  const headers = parseMarkdownTableRow(source[start] ?? "");
  const divider = parseMarkdownTableRow(source[start + 1] ?? "");
  if (!headers || !divider || headers.length !== divider.length || !isDivider(divider)) return null;

  const rows: string[][] = [];
  let cursor = start + 2;
  while (cursor < source.length) {
    const row = compatibleMarkdownTableRow(source[cursor], headers.length);
    if (!row) break;
    rows.push(row);
    cursor += 1;
  }
  if (rows.length === 0) return null;

  const available = Math.max(24, width);
  const ambiguousWidth = [...headers, ...rows.flat()].some(hasAmbiguousTerminalWidth);
  if (ambiguousWidth || available < 72 || headers.length > 5) {
    return { consumed: cursor - start, mode: "stacked", lines: renderStacked(headers, rows, available) };
  }

  const alignments = divider.map((cell): "left" | "center" | "right" => {
    const compact = cell.replace(/\s/g, "");
    if (compact.startsWith(":") && compact.endsWith(":")) return "center";
    if (compact.endsWith(":")) return "right";
    return "left";
  });
  const widths = headers.map((header, index) => Math.max(
    6,
    terminalWidth(header),
    ...rows.map((row) => terminalWidth(row[index] ?? "")),
  ));
  const cellBudget = available - (3 * widths.length) - 1;
  if (cellBudget < widths.length * 6) {
    return { consumed: cursor - start, mode: "stacked", lines: renderStacked(headers, rows, available) };
  }
  while (widths.reduce((sum, value) => sum + value, 0) > cellBudget) {
    const largest = widths.reduce((best, value, index) => value > widths[best] ? index : best, 0);
    if (widths[largest] <= 6) break;
    widths[largest] -= 1;
  }

  const border = (left: string, middle: string, right: string) =>
    left + widths.map((cellWidth) => "─".repeat(cellWidth + 2)).join(middle) + right;
  const output = [border("┌", "┬", "┐")];
  const renderRow = (row: string[], header = false) => {
    const wrapped = row.map((cell, index) => wrapCell(cell, widths[index]));
    const height = Math.max(...wrapped.map((cell) => cell.length));
    for (let line = 0; line < height; line += 1) {
      output.push("│ " + wrapped.map((cell, index) =>
        pad(cell[line] ?? "", widths[index], header ? "center" : alignments[index]),
      ).join(" │ ") + " │");
    }
  };
  renderRow(headers, true);
  output.push(border("├", "┼", "┤"));
  rows.forEach((row, index) => {
    renderRow(row);
    if (index < rows.length - 1) output.push(border("├", "┼", "┤"));
  });
  output.push(border("└", "┴", "┘"));
  return { consumed: cursor - start, mode: "grid", lines: output };
}
