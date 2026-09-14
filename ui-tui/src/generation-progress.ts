import type { GenerationProgress } from "./types.js";

export function generationProgressLabel(progress: GenerationProgress | null): string | undefined {
  if (!progress || progress.phase === "finished") return undefined;
  if (progress.phase === "waiting") return `NO OUTPUT ${Math.floor(progress.idle_seconds)}s`;
  return `${progress.phase === "requesting" ? "MODEL WAIT" : "GENERATING"} ${Math.floor(progress.elapsed_seconds)}s`;
}
