import React from "react";
import { Text, Box } from "ink";
import type { ChatMessage } from "../types.js";
import { useTheme } from "../theme-context.js";

interface Props {
  messages: ChatMessage[];
}

export function ChatLog({ messages }: Props) {
  const theme = useTheme();
  return (
    <Box flexDirection="column" flexGrow={1}>
      {messages.map((msg, index) => (
        <Box key={msg.id ?? index}>
          <Text color={msg.role === "user" ? theme.accent : msg.role === "assistant" ? theme.text : theme.muted}>
            {msg.content}
          </Text>
        </Box>
      ))}
    </Box>
  );
}
