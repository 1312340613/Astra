const DEFAULT_IMAGE_PROMPT = "Describe this image.";
const IMAGE_EXTENSIONS = /\.(png|jpe?g|webp|gif)$/i;
const WINDOWS_ABSOLUTE_PATH = /^[A-Za-z]:[\\/]/;
const QUOTED_IMAGE_PATH = /(["'])([^"']+\.(?:png|jpe?g|webp|gif))\1/gi;
const WINDOWS_IMAGE_PATH = /([A-Za-z]:[\s\S]*?\.(?:png|jpe?g|webp|gif))(?=[A-Za-z]:|\s|$|["'])/gi;
const OTHER_IMAGE_PATH = /(?<!\S)([./~]?[^\s"']+[\\/][^\s"']+\.(?:png|jpe?g|webp|gif))/gi;

type ImageSpan = {
  path: string;
  start: number;
  end: number;
};

type ImageInputItem = {
  path: string;
  start: number;
  end: number;
};

export type ImageInputAttachment = {
  label: string;
  path: string;
};

export function insertImageInputValue(current: string, text: string, cursor: number): {
  value: string;
  cursor: number;
} {
  const offset = Math.max(0, Math.min(cursor, current.length));
  const before = current.slice(0, offset);
  const after = current.slice(offset);
  // Preserve the paste boundary before the draft parser can mistake adjacent
  // prose for a relative path (e.g. "哈" + "/tmp/photo.png"). Ordinary typing
  // and mixed text pastes retain their normal insertion behavior.
  const spans = IMAGE_EXTENSIONS.test(text.trim().replace(/["']$/, "")) ? extractImagePaths(text) : [];
  const imageOnly = spans.length > 0 && removeSpans(text, spans).trim() === "";
  const leading = imageOnly && /\S$/.test(before) && /^\S/.test(text) ? " " : "";
  const trailing = imageOnly && /\S$/.test(text) && /^\S/.test(after) ? " " : "";
  const inserted = leading + text + trailing;
  return { value: before + inserted + after, cursor: offset + inserted.length };
}

function parsePathAndPrompt(text: string): { path: string; prompt: string } | null {
  const match = text.match(/^("([^"]+)"|'([^']+)'|(\S+))(?:\s+([\s\S]*))?$/);
  if (!match) return null;
  return {
    path: match[2] ?? match[3] ?? match[4] ?? "",
    prompt: (match[5] ?? DEFAULT_IMAGE_PROMPT).trim() || DEFAULT_IMAGE_PROMPT,
  };
}

export function parseImageInput(text: string): { path: string; prompt: string } | null {
  const trimmed = text.trim();
  if (!trimmed) return null;

  if (trimmed.startsWith("/image")) {
    const payload = trimmed.slice("/image".length).trim();
    return parsePathAndPrompt(payload);
  }

  const parsed = parsePathAndPrompt(trimmed);
  if (!parsed) return null;
  if (!IMAGE_EXTENSIONS.test(parsed.path)) return null;
  const explicitPath =
    parsed.prompt === DEFAULT_IMAGE_PROMPT
    || /^["']/.test(trimmed)
    || WINDOWS_ABSOLUTE_PATH.test(parsed.path)
    || parsed.path.startsWith(".")
    || parsed.path.includes("/")
    || parsed.path.includes("\\");
  if (!explicitPath) return null;
  return parsed;
}

export function formatImageInputDisplay(text: string): string | null {
  const attachments = extractImagePaths(text);
  if (attachments.length === 0) return null;

  let prompt = removeSpans(text, attachments).trim();
  prompt = prompt.replace(/\s+/g, " ");
  prompt = prompt.replace(/^\/image\b/i, "").trim();
  const labels = attachments.map((_attachment, index) => `[Image #${index + 1}]`).join(" ");
  return prompt ? `${prompt}\n${labels}` : labels;
}

export function normalizeImageInputValue(text: string): {
  displayText: string;
  attachments: ImageInputAttachment[];
} | null {
  const spans = extractImagePaths(text);
  if (spans.length === 0) return null;
  return updateImageInputValue(text, []);
}

export function updateImageInputValue(
  text: string,
  currentAttachments: ImageInputAttachment[],
): {
  displayText: string;
  attachments: ImageInputAttachment[];
} {
  const cleanedText = removeBrokenImageLabels(text, currentAttachments);
  const spans = extractImagePaths(cleanedText);
  const items = [
    ...extractExistingImageLabels(cleanedText, currentAttachments),
    ...spans.map((span) => ({ path: span.path, start: span.start, end: span.end })),
  ].sort((a, b) => a.start - b.start);

  if (items.length === 0) {
    return { displayText: cleanedText, attachments: [] };
  }

  let promptBase = removeSpans(cleanedText, spans);
  for (const attachment of currentAttachments) {
    promptBase = promptBase.split(attachment.label).join(" ");
  }

  const prompt = normalizePrompt(promptBase);
  const attachments = items.map((item, index) => ({
    label: `[Image #${index + 1}]`,
    path: item.path,
  }));
  const labels = attachments.map((attachment) => attachment.label).join(" ");
  return {
    displayText: prompt ? `${labels} ${prompt}` : labels,
    attachments,
  };
}

export function shouldNormalizeImageInputValue(
  text: string,
  currentAttachments: ImageInputAttachment[],
): boolean {
  if (extractImagePaths(text).length > 0) return true;
  return currentAttachments.some((attachment) => isBrokenImageLabel(text, attachment));
}

export function resolveImageInputSubmitText(text: string, attachments: ImageInputAttachment[]): string {
  let result = text;
  for (const attachment of attachments) {
    result = result.split(attachment.label).join(attachment.path);
  }
  return result.replace(/\s+/g, " ").trim();
}

export function normalizeMultilineInputValue(text: string): string {
  return text
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .join(" / ");
}

function extractImagePaths(text: string): ImageSpan[] {
  const found: ImageSpan[] = [];
  const occupied: Array<[number, number]> = [];

  for (const match of text.matchAll(QUOTED_IMAGE_PATH)) {
    const path = match[2] ?? "";
    const start = match.index ?? 0;
    const end = start + match[0].length;
    found.push({ path, start, end });
    occupied.push([start, end]);
  }

  for (const match of text.matchAll(WINDOWS_IMAGE_PATH)) {
    const path = match[1] ?? "";
    const start = match.index ?? 0;
    const end = start + match[0].length;
    if (overlapsAny(start, end, occupied)) continue;
    found.push({ path, start, end });
    occupied.push([start, end]);
  }

  for (const match of text.matchAll(OTHER_IMAGE_PATH)) {
    const path = match[1] ?? "";
    const start = match.index ?? 0;
    const end = start + match[0].length;
    if (overlapsAny(start, end, occupied)) continue;
    found.push({ path, start, end });
    occupied.push([start, end]);
  }

  return found.sort((a, b) => a.start - b.start);
}

function extractExistingImageLabels(
  text: string,
  attachments: ImageInputAttachment[],
): ImageInputItem[] {
  const items: ImageInputItem[] = [];
  for (const attachment of attachments) {
    let start = text.indexOf(attachment.label);
    while (start >= 0) {
      items.push({ path: attachment.path, start, end: start + attachment.label.length });
      start = text.indexOf(attachment.label, start + attachment.label.length);
    }
  }
  return items;
}

function removeBrokenImageLabels(text: string, attachments: ImageInputAttachment[]): string {
  let result = text;
  for (const attachment of attachments) {
    if (result.includes(attachment.label)) continue;
    const partial = findBrokenImageLabel(result, attachment);
    if (partial) {
      result = result.split(partial).join(" ");
    }
  }
  return result.replace(/\s+/g, " ").trim();
}

function isBrokenImageLabel(text: string, attachment: ImageInputAttachment): boolean {
  return !text.includes(attachment.label) && findBrokenImageLabel(text, attachment) !== "";
}

function findBrokenImageLabel(text: string, attachment: ImageInputAttachment): string {
  for (let length = attachment.label.length - 1; length >= "[Image #".length; length -= 1) {
    const partial = attachment.label.slice(0, length);
    if (text.includes(partial)) return partial;
  }
  return "";
}

function normalizePrompt(text: string): string {
  return text
    .trim()
    .replace(/\s+/g, " ")
    .replace(/^\/image\b/i, "")
    .trim();
}

function overlapsAny(start: number, end: number, spans: Array<[number, number]>): boolean {
  return spans.some(([spanStart, spanEnd]) => !(end <= spanStart || start >= spanEnd));
}

function removeSpans(text: string, spans: ImageSpan[]): string {
  let result = "";
  let last = 0;
  for (const span of spans) {
    result += text.slice(last, span.start);
    last = span.end;
  }
  return result + text.slice(last);
}
