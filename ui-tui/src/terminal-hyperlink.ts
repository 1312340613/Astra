import { posix, win32 } from "node:path";

const MAX_TERMINAL_HREF_LENGTH = 4096;
const TERMINAL_CONTROLS = /[\u0000-\u001f\u007f-\u009f]/u;
const OSC_8_PREFIX = "\x1b]8;;";
const OSC_TERMINATOR = "\x07";

export type TerminalHyperlinkMode = "osc8" | "visible-target";

type LocalTarget = {
  path: string;
  line?: number;
  column?: number;
};

function parsePositiveCoordinate(value: string | undefined): number | undefined | null {
  if (value === undefined) return undefined;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function parseLocalTarget(source: string): LocalTarget | null {
  const lineAndColumn = /^(.*):(-?\d+):(-?\d+)$/u.exec(source);
  const lineOnly = lineAndColumn ? null : /^(.*):(-?\d+)$/u.exec(source);
  const path = lineAndColumn?.[1] ?? lineOnly?.[1] ?? source;
  if ((!path.startsWith("/") && !/^[a-z]:[\\/]/iu.test(path)) || TERMINAL_CONTROLS.test(path)) return null;

  const line = parsePositiveCoordinate(lineAndColumn?.[2] ?? lineOnly?.[2]);
  const column = parsePositiveCoordinate(lineAndColumn?.[3]);
  if (line === null || column === null) return null;
  return { path, line, column };
}

function localTargetToVscode(target: LocalTarget): string {
  // Link syntax has its own path flavor: the host OS must not add a drive
  // to a POSIX path, or interpret Windows backslashes as POSIX filename text.
  const drivePath = target.path.replace(/^\/([a-z]:\/)/iu, "$1");
  const normalizedPath = /^[a-z]:[\\/]/iu.test(drivePath)
    ? `/${win32.normalize(drivePath).replaceAll("\\", "/")}`
    : posix.resolve(target.path);
  const encodedPath = normalizedPath.split("/").map(encodeURIComponent).join("/")
    .replace(/^\/([a-z])%3A\//iu, "/$1:/");
  const lineSuffix = target.line === undefined ? "" : `:${target.line}`;
  const columnSuffix = target.column === undefined ? "" : `:${target.column}`;
  return `vscode://file${encodedPath}${lineSuffix}${columnSuffix}`;
}

export function resolveTerminalHyperlinkTarget(href: string | undefined): string | null {
  if (
    !href
    || href.length > MAX_TERMINAL_HREF_LENGTH
    || href.trim() !== href
    || TERMINAL_CONTROLS.test(href)
  ) return null;

  if (href.startsWith("/") || /^[a-z]:[\\/]/iu.test(href)) {
    const local = parseLocalTarget(href);
    try {
      return local ? localTargetToVscode(local) : null;
    } catch {
      return null;
    }
  }

  let parsed: URL;
  try {
    parsed = new URL(href);
  } catch {
    return null;
  }

  if (parsed.protocol === "http:" || parsed.protocol === "https:" || parsed.protocol === "mailto:") {
    return href;
  }
  if (parsed.protocol !== "file:" || (parsed.hostname && parsed.hostname !== "localhost")) return null;
  if (parsed.search || parsed.hash || /%(?:2f|5c)/iu.test(parsed.pathname)) return null;

  let filePath: string;
  try {
    filePath = decodeURIComponent(parsed.pathname);
  } catch {
    return null;
  }
  const local = parseLocalTarget(filePath);
  return local ? localTargetToVscode(local) : null;
}

export function terminalHyperlinkMode(env: NodeJS.ProcessEnv = process.env): TerminalHyperlinkMode {
  return env.TERM_PROGRAM === "Apple_Terminal" ? "visible-target" : "osc8";
}

export function terminalHyperlinkDisplayText(
  label: string,
  href: string | undefined,
  mode: TerminalHyperlinkMode = terminalHyperlinkMode(),
): string {
  if (mode !== "visible-target") return label;
  const target = resolveTerminalHyperlinkTarget(href);
  return target ? `${label} <${target}>` : label;
}

export function formatTerminalHyperlink(
  label: string,
  href: string | undefined,
  mode: TerminalHyperlinkMode = terminalHyperlinkMode(),
): string {
  if (mode === "visible-target") return label;
  const target = resolveTerminalHyperlinkTarget(href);
  if (!target) return label;
  return `${OSC_8_PREFIX}${target}${OSC_TERMINATOR}${label}${OSC_8_PREFIX}${OSC_TERMINATOR}`;
}
