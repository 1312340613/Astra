import React from "react";
import { Box } from "ink";

export function StreamingControlLayout({
  dynamic,
  header,
  status,
  auxiliary,
  input,
}: {
  dynamic: React.ReactNode;
  header: React.ReactNode;
  status: React.ReactNode;
  auxiliary?: React.ReactNode;
  input: React.ReactNode;
}) {
  return (
    <>
      {dynamic}
      <Box flexDirection="column" flexShrink={0}>{header}</Box>
      <Box flexDirection="column" flexShrink={0}>{status}</Box>
      {auxiliary}
      <Box flexDirection="column" flexShrink={0}>{input}</Box>
    </>
  );
}
