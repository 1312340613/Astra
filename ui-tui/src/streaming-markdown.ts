import { TerminalMarkdownStream, type MarkdownBlock } from "./terminal-markdown.js";
import type { TimeRail } from "./message-time-rail.js";

export type StreamingRole = "reasoning" | "assistant";

export type StreamingUpdate = {
  committed: MarkdownBlock[];
  preview: MarkdownBlock[];
  startingLineIndex: number;
  timeRail?: TimeRail;
};

type RoleState = {
  parser: TerminalMarkdownStream;
  lineIndex: number;
  timeRail?: TimeRail;
};

export class StreamingMarkdownCoordinator {
  private states = new Map<StreamingRole, RoleState>();

  start(role: StreamingRole, timeRail?: TimeRail): void {
    if (!this.states.has(role)) {
      this.states.set(role, {
        parser: new TerminalMarkdownStream(),
        lineIndex: 0,
        timeRail,
      });
    }
  }

  push(role: StreamingRole, chunk: string): StreamingUpdate {
    const state = this.require(role);
    const parsed = state.parser.push(chunk);
    return {
      committed: parsed.committed,
      preview: parsed.preview,
      startingLineIndex: state.lineIndex,
      timeRail: state.timeRail,
    };
  }

  finish(role: StreamingRole): StreamingUpdate {
    const state = this.states.get(role);
    if (!state) {
      return { committed: [], preview: [], startingLineIndex: 0 };
    }

    const committed = state.parser.finish();
    const update = {
      committed,
      preview: [],
      startingLineIndex: state.lineIndex,
      timeRail: state.timeRail,
    };
    this.states.delete(role);
    return update;
  }

  advance(role: StreamingRole, displayLineCount: number): void {
    const state = this.require(role);
    state.lineIndex += displayLineCount;
  }

  clearTimeRails(): void {
    for (const state of this.states.values()) {
      state.timeRail = undefined;
    }
  }

  clear(): void {
    this.states.clear();
  }

  snapshot(role: StreamingRole): { lineIndex: number; timeRail?: TimeRail } | null {
    const state = this.states.get(role);
    return state ? { lineIndex: state.lineIndex, timeRail: state.timeRail } : null;
  }

  private require(role: StreamingRole): RoleState {
    const state = this.states.get(role);
    if (!state) throw new Error(`Streaming role not started: ${role}`);
    return state;
  }
}
