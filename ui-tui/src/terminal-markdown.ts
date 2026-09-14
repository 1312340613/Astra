import {
  compatibleMarkdownTableRow,
  consumeStreamingTableLine,
  parseMarkdownTableRow,
} from "./markdown-table.js";
import { formatLatexForTerminal } from "./markdown-math.js";
import { terminalSafeCut, terminalTextFragments } from "./terminal-text.js";

export const MAX_PENDING_INLINE_CHARS = 8_192;
export const MAX_PENDING_BLOCK_CHARS = 8_192;
export const MAX_PENDING_TABLE_LINES = 256;

export type InlineSpan = {
  kind: "text" | "strong" | "emphasis" | "strikethrough" | "code" | "link" | "math" | "hard_break";
  text: string;
  href?: string;
  malformed?: boolean;
  provisional?: boolean;
};

const BREAK_TAG = /^<br(?:>|\/>| \/>)/iu;
const BREAK_TAG_FORMS = ["<br>", "<br/>", "<br />"];

export type MarkdownBlock =
  | { kind: "paragraph" | "heading" | "quote"; spans: InlineSpan[] }
  | { kind: "list"; ordered: boolean; ordinal?: number; spans: InlineSpan[] }
  | { kind: "blank" | "rule" }
  | { kind: "code"; text: string }
  | { kind: "table"; source: string[] }
  | { kind: "math"; source: string; malformed: boolean };

export type MarkdownParseSnapshot = {
  committed: MarkdownBlock[];
  preview: MarkdownBlock[];
};

function isUnescaped(source: string, index: number): boolean {
  let slashes = 0;
  for (let cursor = index - 1; cursor >= 0 && source[cursor] === "\\"; cursor -= 1) slashes += 1;
  return slashes % 2 === 0;
}

function nextDollar(source: string, from: number): number {
  for (let cursor = from; cursor < source.length; cursor += 1) {
    if (source[cursor] === "$" && source[cursor + 1] !== "$" && isUnescaped(source, cursor)) return cursor;
  }
  return -1;
}

function appendSpan(spans: InlineSpan[], span: InlineSpan): void {
  const previous = spans.at(-1);
  if (span.kind === "text" && previous?.kind === "text") {
    previous.text += span.text;
  } else {
    spans.push(span);
  }
}

function supportsHardBreak(kind: InlineSpan["kind"]): boolean {
  return kind === "strong"
    || kind === "emphasis"
    || kind === "strikethrough"
    || kind === "link";
}

function appendBreakAwareSpan(
  spans: InlineSpan[],
  span: InlineSpan,
  final: boolean,
): void {
  let cursor = 0;
  let text = "";
  const flush = () => {
    if (!text) return;
    appendSpan(spans, { ...span, text });
    text = "";
  };

  while (cursor < span.text.length) {
    const breakTag = span.text.slice(cursor).match(BREAK_TAG);
    if (breakTag) {
      flush();
      appendSpan(spans, { kind: "hard_break", text: "" });
      cursor += breakTag[0].length;
      continue;
    }
    const remaining = span.text.slice(cursor).toLowerCase();
    if (!final && remaining.startsWith("<") && BREAK_TAG_FORMS.some((tag) => tag.startsWith(remaining))) {
      flush();
      spans.push({ ...span, text: "", provisional: true });
      return;
    }
    text += span.text[cursor];
    cursor += 1;
  }
  flush();
}

export function parseInlineMarkdown(source: string, final: boolean): InlineSpan[] {
  const spans: InlineSpan[] = [];

  for (let cursor = 0; cursor < source.length;) {
    const breakTag = source.slice(cursor).match(BREAK_TAG);
    if (breakTag) {
      appendSpan(spans, { kind: "hard_break", text: "" });
      cursor += breakTag[0].length;
      continue;
    }
    const remaining = source.slice(cursor).toLowerCase();
    if (!final && remaining.startsWith("<") && BREAK_TAG_FORMS.some((tag) => tag.startsWith(remaining))) {
      spans.push({ kind: "text", text: "", provisional: true });
      break;
    }

    let kind: InlineSpan["kind"] | undefined;
    let opener = "";
    let closer = "";
    let end = -1;
    let href: string | undefined;

    if (source[cursor] === "`") {
      kind = "code";
      opener = "`";
      closer = "`";
      end = source.indexOf(closer, cursor + opener.length);
    } else if (source.startsWith("\\(", cursor)) {
      kind = "math";
      opener = "\\(";
      closer = "\\)";
      end = source.indexOf(closer, cursor + opener.length);
    } else if (
      source[cursor] === "$"
      && source[cursor + 1] !== "$"
      && isUnescaped(source, cursor)
    ) {
      kind = "math";
      opener = "$";
      closer = "$";
      end = nextDollar(source, cursor + opener.length);
    } else if (source.startsWith("**", cursor) || source.startsWith("__", cursor)) {
      kind = "strong";
      opener = source.slice(cursor, cursor + 2);
      closer = opener;
      end = source.indexOf(closer, cursor + opener.length);
    } else if (source.startsWith("~~", cursor)) {
      kind = "strikethrough";
      opener = "~~";
      closer = "~~";
      end = source.indexOf(closer, cursor + opener.length);
    } else if (source[cursor] === "*") {
      kind = "emphasis";
      opener = "*";
      closer = "*";
      end = source.indexOf(closer, cursor + opener.length);
    } else if (source[cursor] === "[") {
      kind = "link";
      opener = "[";
      const labelEnd = source.indexOf("](", cursor + opener.length);
      if (labelEnd >= 0) {
        const targetEnd = source.indexOf(")", labelEnd + 2);
        if (targetEnd >= 0) {
          end = targetEnd;
          href = source.slice(labelEnd + 2, targetEnd);
          closer = ")";
        }
      }
    }

    if (!kind) {
      appendSpan(spans, { kind: "text", text: source[cursor] });
      cursor += 1;
      continue;
    }

    if (end >= 0 && end + closer.length - cursor <= MAX_PENDING_INLINE_CHARS) {
      const text = kind === "link"
        ? source.slice(cursor + opener.length, source.indexOf("](", cursor + opener.length))
        : source.slice(cursor + opener.length, end);
      if (!text || (kind === "link" && !href)) {
        const preserveCloserAsOpener = !text
          && opener === closer
          && opener.length === 1
          && source.indexOf(closer, end + closer.length) >= 0;
        const literalEnd = preserveCloserAsOpener ? cursor + opener.length : end + closer.length;
        appendSpan(spans, { kind: "text", text: source.slice(cursor, literalEnd) });
        cursor = literalEnd;
        continue;
      }
      if (kind === "math") {
        const formatted = formatLatexForTerminal(text);
        appendSpan(spans, { kind, text: formatted.text, malformed: formatted.malformed });
      } else if (supportsHardBreak(kind)) {
        appendBreakAwareSpan(spans, { kind, text, href }, final);
      } else {
        appendSpan(spans, { kind, text, href });
      }
      cursor = end + closer.length;
      continue;
    }

    const unresolvedLength = source.length - cursor;
    if (final) {
      appendSpan(spans, { kind: "text", text: opener });
      cursor += opener.length;
      continue;
    }
    if (unresolvedLength > MAX_PENDING_INLINE_CHARS) {
      appendSpan(spans, { kind: "text", text: source.slice(cursor) });
    } else {
      const provisionalSpan: InlineSpan = {
        kind,
        text: source.slice(cursor + opener.length),
        provisional: true,
      };
      if (supportsHardBreak(kind)) appendBreakAwareSpan(spans, provisionalSpan, false);
      else appendSpan(spans, provisionalSpan);
    }
    break;
  }

  return spans;
}

function ordinaryLine(line: string, final: boolean): MarkdownBlock {
  const trimmed = line.trim();
  if (!trimmed) return { kind: "blank" };

  const heading = line.match(/^ {0,3}(#{1,6})[ \t]+(.+)/);
  if (heading) return { kind: "heading", spans: parseInlineMarkdown(heading[2], final) };

  if (/^[-*_]{3,}$/.test(trimmed)) return { kind: "rule" };

  const quote = line.match(/^>\s?(.*)/);
  if (quote) return { kind: "quote", spans: parseInlineMarkdown(quote[1], final) };

  const unordered = line.match(/^\s*[-*]\s+(.+)/);
  if (unordered) return { kind: "list", ordered: false, spans: parseInlineMarkdown(unordered[1], final) };

  const ordered = line.match(/^\s*(\d+)\.\s+(.+)/);
  if (ordered) {
    return {
      kind: "list",
      ordered: true,
      ordinal: Number(ordered[1]),
      spans: parseInlineMarkdown(ordered[2], final),
    };
  }

  return { kind: "paragraph", spans: parseInlineMarkdown(line, final) };
}

function literalLine(line: string): MarkdownBlock {
  return { kind: "paragraph", spans: [{ kind: "text", text: line }] };
}

function mathBlock(source: string, malformed = false): MarkdownBlock {
  return { kind: "math", source, malformed: malformed || formatLatexForTerminal(source).malformed };
}

export class TerminalMarkdownStream {
  private pendingLine = "";
  private inFence = false;
  private mathDelimiter: "" | "$$" | "\\]" = "";
  private mathOpener = "";
  private pendingMath: string[] = [];
  private pendingTable: string[] = [];
  private releasedLineKind: "" | "literal" | "code" = "";
  private releasedMathDelimiter: "" | "$$" | "\\]" = "";
  private releasedTableColumns: number | null = null;

  push(chunk: string): MarkdownParseSnapshot {
    const committed: MarkdownBlock[] = [];
    let cursor = 0;
    while (cursor < chunk.length) {
      const newline = chunk.indexOf("\n", cursor);
      const end = newline >= 0 ? newline : chunk.length;
      this.appendSegment(chunk.slice(cursor, end), committed, newline >= 0);
      if (newline < 0) break;
      this.completePendingLine(committed);
      cursor = newline + 1;
    }

    return { committed, preview: this.preview() };
  }

  preview(): MarkdownBlock[] {
    const preview: MarkdownBlock[] = [];
    // Open tables render as plain pipe text in live preview. Boxed layout is
    // painted once by the committed block; if the terminal fails to erase a
    // preview frame (seen as ghost half-tables on Apple Terminal), the stale
    // residue is harmless pipe text instead of a convincing fake table.
    preview.push(...this.pendingTable.map((line) => ordinaryLine(line, true)));

    if (this.releasedLineKind) return preview;

    if (this.releasedMathDelimiter) {
      if (this.pendingLine && !this.isDelimiterPrefix(this.pendingLine, this.releasedMathDelimiter)) {
        preview.push(literalLine(this.pendingLine));
      }
      return preview;
    }

    if (this.inFence) {
      if (this.pendingLine && !this.isFenceDelimiterPrefix(this.pendingLine)) {
        preview.push({ kind: "code", text: this.pendingLine });
      }
      return preview;
    }

    if (this.mathDelimiter) {
      const pending = this.isDelimiterPrefix(this.pendingLine, this.mathDelimiter)
        ? this.pendingMath
        : [...this.pendingMath, this.pendingLine].filter((line, index, lines) => line || index < lines.length - 1);
      if (pending.length > 0) preview.push(mathBlock(pending.join("\n"), true));
      return preview;
    }

    const trimmed = this.pendingLine.trim();
    if (/^ {0,3}#{1,6}\s*$/u.test(this.pendingLine) || this.isFenceDelimiterPrefix(this.pendingLine)) {
      return preview;
    }
    if (trimmed === "$" || trimmed === "$$") return preview;
    if (trimmed.startsWith("$$")) {
      let source = trimmed.slice(2);
      if (source.endsWith("$$")) source = source.slice(0, -2);
      else if (source.endsWith("$")) source = source.slice(0, -1);
      if (source) preview.push(mathBlock(source, true));
      return preview;
    }
    if (trimmed === "\\" || trimmed === "\\[") return preview;
    if (trimmed.startsWith("\\[")) {
      let source = trimmed.slice(2);
      if (source.endsWith("\\]")) source = source.slice(0, -2);
      else if (source.endsWith("\\")) source = source.slice(0, -1);
      if (source) preview.push(mathBlock(source, true));
      return preview;
    }

    if (this.pendingLine) {
      preview.push(ordinaryLine(this.pendingLine, false));
    }
    return preview;
  }

  finish(): MarkdownBlock[] {
    const committed: MarkdownBlock[] = [];
    if (this.releasedLineKind) {
      if (this.pendingLine) {
        committed.push(...this.fragmentBlocks(this.pendingLine, this.releasedLineKind));
      }
      this.pendingLine = "";
      this.releasedLineKind = "";
    } else if (this.pendingLine) {
      const line = this.pendingLine;
      this.pendingLine = "";
      committed.push(...this.consumeLine(line, true));
    }

    committed.push(...this.flushPendingTable());
    if (this.mathDelimiter) {
      committed.push(mathBlock(this.pendingMath.join("\n"), true));
    }

    this.pendingLine = "";
    this.inFence = false;
    this.mathDelimiter = "";
    this.mathOpener = "";
    this.pendingMath = [];
    this.pendingTable = [];
    this.releasedLineKind = "";
    this.releasedMathDelimiter = "";
    this.releasedTableColumns = null;
    return committed;
  }

  private appendSegment(segment: string, committed: MarkdownBlock[], lineComplete: boolean): void {
    if (!segment) return;

    let cursor = 0;
    while (cursor < segment.length) {
      const remaining = MAX_PENDING_BLOCK_CHARS - this.pendingLine.length;
      if (remaining === 0) {
        this.releasePendingLineSafely(
          committed,
          segment.slice(cursor, cursor + MAX_PENDING_BLOCK_CHARS),
        );
        continue;
      }

      const take = Math.min(remaining, segment.length - cursor);
      this.pendingLine += segment.slice(cursor, cursor + take);
      cursor += take;

      if (this.releasedMathDelimiter) {
        if (!this.isDelimiterPrefix(this.pendingLine, this.releasedMathDelimiter)) {
          this.releasePendingLine(committed);
        }
        continue;
      }

      const mathChars = this.mathDelimiter
        ? this.mathBufferedChars() + this.pendingLine.length
        : 0;
      if (this.mathDelimiter && mathChars > MAX_PENDING_BLOCK_CHARS) {
        if (lineComplete && cursor === segment.length) {
          this.releasePendingLine(committed);
        } else {
          this.releasePendingLineSafely(
            committed,
            segment.slice(cursor, cursor + MAX_PENDING_BLOCK_CHARS),
            cursor === segment.length,
          );
        }
        continue;
      }

      const tableChars = this.pendingTableChars() + this.pendingLine.length;
      if (this.pendingTable.length > 0 && tableChars > MAX_PENDING_BLOCK_CHARS) {
        if (lineComplete && cursor === segment.length) {
          this.releasePendingLine(committed);
        } else {
          this.releasePendingLineSafely(
            committed,
            segment.slice(cursor, cursor + MAX_PENDING_BLOCK_CHARS),
            cursor === segment.length,
          );
        }
        continue;
      }

      if (cursor < segment.length && this.pendingLine.length === MAX_PENDING_BLOCK_CHARS) {
        this.releasePendingLineSafely(
          committed,
          segment.slice(cursor, cursor + MAX_PENDING_BLOCK_CHARS),
        );
      }
    }
  }

  private releasePendingLineSafely(
    committed: MarkdownBlock[],
    lookahead: string,
    holdTrailingToken = false,
  ): void {
    const cut = terminalSafeCut(
      this.pendingLine,
      this.pendingLine.length,
      lookahead,
      holdTrailingToken && !lookahead,
    );
    if (cut === 0 && (this.mathDelimiter || this.pendingTable.length > 0)) {
      const carry = this.pendingLine;
      this.pendingLine = "";
      this.releasePendingLine(committed);
      this.pendingLine = carry;
      return;
    }
    const safeCut = cut || this.pendingLine.length;
    const carry = this.pendingLine.slice(safeCut);
    this.pendingLine = this.pendingLine.slice(0, safeCut);
    this.releasePendingLine(committed);
    this.pendingLine = carry;
  }

  private releasePendingLine(committed: MarkdownBlock[]): void {
    if (this.releasedMathDelimiter) {
      committed.push(...this.fragmentBlocks(this.pendingLine, "literal"));
      this.pendingLine = "";
      this.releasedLineKind = "literal";
      return;
    }

    if (this.mathDelimiter) {
      committed.push(...this.releaseOpenMath(this.pendingLine));
      this.pendingLine = "";
      this.releasedLineKind = "literal";
      return;
    }

    if (this.pendingTable.length > 0) {
      committed.push(...this.releasePendingTable(), ...this.fragmentBlocks(this.pendingLine, "literal"));
      this.pendingLine = "";
      this.releasedLineKind = "literal";
      return;
    }

    const kind = this.inFence ? "code" : "literal";
    committed.push(...this.fragmentBlocks(this.pendingLine, kind));
    this.pendingLine = "";
    this.releasedLineKind = kind;
  }

  private completePendingLine(committed: MarkdownBlock[]): void {
    if (this.releasedLineKind) {
      if (this.pendingLine) {
        committed.push(...this.fragmentBlocks(this.pendingLine, this.releasedLineKind));
      }
      this.pendingLine = "";
      this.releasedLineKind = "";
      return;
    }

    const line = this.pendingLine;
    this.pendingLine = "";
    committed.push(...this.consumeLine(line, true));
  }

  private consumeLine(line: string, final: boolean): MarkdownBlock[] {
    const trimmed = line.trim();

    if (this.releasedMathDelimiter) {
      const closes = trimmed === this.releasedMathDelimiter;
      if (closes) this.releasedMathDelimiter = "";
      return [literalLine(line)];
    }

    if (this.releasedTableColumns !== null) {
      if (compatibleMarkdownTableRow(line, this.releasedTableColumns)) {
        return [literalLine(line)];
      }
      this.releasedTableColumns = null;
    }

    if (this.inFence) {
      if (trimmed.startsWith("```")) {
        this.inFence = false;
        return [];
      }
      return [{ kind: "code", text: line || " " }];
    }

    if (trimmed.startsWith("```")) {
      const flushed = this.flushPendingTable();
      this.inFence = true;
      return flushed;
    }

    if (this.mathDelimiter) {
      if (trimmed === this.mathDelimiter) {
        const block = mathBlock(this.pendingMath.join("\n"));
        this.mathDelimiter = "";
        this.mathOpener = "";
        this.pendingMath = [];
        return [block];
      }
      this.pendingMath.push(line);
      if (this.mathBufferedChars() > MAX_PENDING_BLOCK_CHARS) {
        return this.releaseOpenMath();
      }
      return [];
    }

    const oneLineDollarMath = trimmed.match(/^\$\$([\s\S]*)\$\$$/);
    const oneLineBracketMath = trimmed.match(/^\\\[([\s\S]*)\\\]$/);
    if (oneLineDollarMath || oneLineBracketMath) {
      return [...this.flushPendingTable(), mathBlock((oneLineDollarMath || oneLineBracketMath)![1])];
    }

    if (trimmed === "$$" || trimmed === "\\[") {
      const flushed = this.flushPendingTable();
      this.mathDelimiter = trimmed === "$$" ? "$$" : "\\]";
      this.mathOpener = line;
      this.pendingMath = [];
      return flushed;
    }

    const decision = consumeStreamingTableLine(this.pendingTable, line);
    this.pendingTable = decision.pending;
    const blocks = decision.blocks.flatMap((block) => block.kind === "table"
      ? [{ kind: "table", source: block.lines } satisfies MarkdownBlock]
      : block.lines.map((text) => ordinaryLine(text, final)));
    if (
      this.pendingTable.length > MAX_PENDING_TABLE_LINES
      || this.pendingTableChars() > MAX_PENDING_BLOCK_CHARS
    ) {
      blocks.push(...this.releasePendingTable());
    }
    return blocks;
  }

  private flushPendingTable(): MarkdownBlock[] {
    if (this.pendingTable.length === 0) return [];
    const pending = this.pendingTable;
    this.pendingTable = [];
    return pending.length >= 2
      ? [{ kind: "table", source: pending }]
      : pending.map((line) => ordinaryLine(line, true));
  }

  private fragmentBlocks(text: string, kind: "literal" | "code"): MarkdownBlock[] {
    return terminalTextFragments(text, MAX_PENDING_BLOCK_CHARS).map((fragment) =>
      kind === "code" ? { kind: "code", text: fragment } : literalLine(fragment)
    );
  }

  private mathBufferedChars(): number {
    return this.mathOpener.length
      + this.pendingMath.reduce((total, line) => total + line.length + 1, 0);
  }

  private pendingTableChars(): number {
    return this.pendingTable.reduce((total, line) => total + line.length + 1, 0);
  }

  private releaseOpenMath(incompleteLine?: string): MarkdownBlock[] {
    const delimiter = this.mathDelimiter;
    if (!delimiter) return [];
    const source = [this.mathOpener, ...this.pendingMath];
    if (incompleteLine) source.push(incompleteLine);
    this.mathDelimiter = "";
    this.mathOpener = "";
    this.pendingMath = [];
    this.releasedMathDelimiter = delimiter;
    return source.flatMap((line) => this.fragmentBlocks(line, "literal"));
  }

  private releasePendingTable(): MarkdownBlock[] {
    if (this.pendingTable.length === 0) return [];
    const pending = this.pendingTable;
    const header = parseMarkdownTableRow(pending[0]);
    this.pendingTable = [];
    this.releasedTableColumns = header?.length ?? null;
    return pending.flatMap((line) => this.fragmentBlocks(line, "literal"));
  }

  private isFenceDelimiterPrefix(line: string): boolean {
    const trimmed = line.trim();
    return /^`{1,2}$/u.test(trimmed) || trimmed.startsWith("```");
  }

  private isDelimiterPrefix(line: string, delimiter: "$$" | "\\]"): boolean {
    const trimmed = line.trim();
    return Boolean(trimmed) && delimiter.startsWith(trimmed);
  }
}

export function parseCompleteMarkdown(source: string): MarkdownBlock[] {
  const parser = new TerminalMarkdownStream();
  const snapshot = parser.push(source);
  return [...snapshot.committed, ...parser.finish()];
}
