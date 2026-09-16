import React, { createContext, useCallback, useContext, useState } from "react";
import { Box, type DOMElement } from "ink";

const LiveRows = createContext(30);
export const useLiveRows = () => useContext(LiveRows);

// Ink 5 exposes its Yoga node but not a maxHeight style. Keep the initial and
// resized frames bounded with height until the ref installs the Yoga limit.
// Thereafter short content keeps its natural height (no blank screen padding).
function BoundedColumn({ limit, children }: { limit: number; children: React.ReactNode }) {
  const [configured, setConfigured] = useState<number>();
  const ref = useCallback((node: DOMElement | null) => {
    if (!node?.yogaNode) return;
    node.yogaNode.setMaxHeight(limit);
    setConfigured(limit);
  }, [limit]);
  return <Box ref={ref} flexDirection="column" flexShrink={0} overflowY="hidden"
    height={configured === limit ? undefined : limit}>{children}</Box>;
}

export function StreamingControlLayout({
  rows = 30,
  interactionActive = false,
  dynamic,
  header,
  status,
  auxiliary,
  input,
}: {
  rows?: number;
  interactionActive?: boolean;
  dynamic: React.ReactNode;
  header: React.ReactNode;
  status: React.ReactNode;
  auxiliary?: React.ReactNode;
  input: React.ReactNode;
}) {
  // Ink appends a newline and replays *all* Static output when outputHeight
  // reaches rows. Yoga budgets actual wrapped control heights in this same
  // layout pass; measuring in an effect would leave the first frame unsafe.
  const height = Math.max(1, rows - 2);
  return (
    <LiveRows.Provider value={height}>
      <BoundedColumn limit={height}>
        <Box flexDirection="column" flexShrink={1} minHeight={0} overflowY="hidden" justifyContent="flex-end">
          <Box flexDirection="column" flexShrink={0}>{interactionActive ? null : dynamic}</Box>
        </Box>
        <BoundedColumn limit={height}>
          <Box flexDirection="column" flexShrink={1} minHeight={0} overflowY="hidden">
            <Box flexDirection="column" flexShrink={0}>{interactionActive ? null : <>{header}{status}</>}</Box>
          </Box>
          <Box flexDirection="column" flexShrink={1} minHeight={0} overflowY="hidden">{auxiliary}</Box>
          <Box flexDirection="column" flexShrink={0} display={interactionActive ? "none" : "flex"}>{input}</Box>
        </BoundedColumn>
      </BoundedColumn>
    </LiveRows.Provider>
  );
}
