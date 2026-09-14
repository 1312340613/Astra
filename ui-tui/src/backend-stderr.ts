const MACOS_MALLOC_STACK_LOGGING_DIAGNOSTIC =
  /^[A-Za-z0-9._+-]+\(\d+\) MallocStackLogging: can't turn off malloc stack logging because it was not enabled\.?$/;

/** Suppress only the known harmless macOS allocator diagnostic. */
export function isBenignMacOSAllocatorDiagnostic(
  line: string,
  platform: NodeJS.Platform = process.platform,
): boolean {
  return (
    platform === "darwin" &&
    MACOS_MALLOC_STACK_LOGGING_DIAGNOSTIC.test(line.trim())
  );
}

/** tqdm writes progress to stderr too. Only complete, recognized progress lines qualify. */
export function isBackendModelProgress(line: string): boolean {
  return /^(?:Fetching \d+ files|Loading checkpoint shards|Loading weights|Downloading(?: (?:shards|[\w./-]+))?|[\w./-]+\.(?:safetensors|bin|json|model|pt|pth)):\s*(?:100|\d{1,2})%\|[^|\r\n]*\|\s*[\d.,]+[kMGT]?\/[\d.,]+[kMGT]?\s*\[[\d:?]+<[^\]\r\n]*\]\s*$/.test(line.trim());
}
