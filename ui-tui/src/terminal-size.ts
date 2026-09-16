import { useLayoutEffect, useState } from "react";

export function useTerminalSize(output: NodeJS.WriteStream) {
  const read = () => ({ columns: output.columns || 100, rows: output.rows || 30 });
  const [size, setSize] = useState(read);
  useLayoutEffect(() => {
    const resize = () => setSize(read());
    // Update the bounded layout before Ink recalculates it against new rows.
    output.prependListener("resize", resize);
    resize();
    return () => { output.off("resize", resize); };
  }, [output]);
  return size;
}
