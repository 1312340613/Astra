import React, { createContext, useContext } from "react";
import { THEMES, type UiTheme } from "./theme.js";

const ThemeContext = createContext<UiTheme>(THEMES.hermes);

export function ThemeProvider({ theme, children }: { theme: UiTheme; children: React.ReactNode }) {
  return <ThemeContext.Provider value={theme}>{children}</ThemeContext.Provider>;
}

export function useTheme(): UiTheme {
  return useContext(ThemeContext);
}
