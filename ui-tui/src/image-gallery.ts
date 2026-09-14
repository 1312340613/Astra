import { execFile } from "node:child_process";
import { isAbsolute, basename, dirname } from "node:path";
import type { ToolResultRecord } from "./tool-results.js";

export type ImageSearchResult = {
  type: "image_search";
  images: Array<{ id: string; title: string; source_url: string; image_url: string }>;
  gallery_path?: string;
};

export function parseImageSearch(result: Pick<ToolResultRecord, "name" | "output" | "error">): ImageSearchResult | null {
  if (result.name !== "search_images" || result.error) return null;
  try {
    const value = JSON.parse(result.output);
    if (value?.type !== "image_search" || value.success !== true || !Array.isArray(value.images) || value.images.length > 10) return null;
    if (!value.images.every((image: any) => image && ["id", "title", "source_url", "image_url"].every((key) => typeof image[key] === "string"))) return null;
    return value;
  } catch { return null; }
}

export function imageSearchSummary(result: ToolResultRecord): string | null {
  const images = parseImageSearch(result);
  if (!images) return null;
  const clean = (text: string) => text.replace(/[\u0000-\u001f\u007f-\u009f]/g, " ");
  const gallery = typeof images.gallery_path === "string" ? ` · /gallery ${result.id} 打开图库` : "";
  return `找到 ${images.images.length} 张图片${gallery}\n` + images.images.map((image, index) =>
    `${index + 1}. ${clean(image.title)}\n   ${clean(image.source_url)}`,
  ).join("\n");
}

export function galleryOpenCommand(path: string, platform: NodeJS.Platform = process.platform): [string, string[]] {
  if (!isAbsolute(path) || /[\u0000-\u001f\u007f]/u.test(path)
    || basename(dirname(path)) !== "image-galleries" || !/^images-[a-f0-9]{32}\.html$/u.test(basename(path))) {
    throw new Error("Invalid image gallery path");
  }
  if (platform === "darwin") return ["open", [path]];
  if (platform === "win32") return ["rundll32.exe", ["url.dll,FileProtocolHandler", path]];
  return ["xdg-open", [path]];
}

export async function openImageGallery(results: ToolResultRecord[], argument?: string): Promise<string> {
  if (argument && !/^[1-9]\d*$/u.test(argument)) return "用法：/gallery [工具结果编号]";
  const result = argument ? results.find((item) => item.id === Number(argument))
    : [...results].reverse().find((item) => parseImageSearch(item)?.gallery_path);
  const path = result && parseImageSearch(result)?.gallery_path;
  if (typeof path !== "string") return "没有可打开的图片图库。先让 Astra 搜图，再使用 /gallery。";
  const [command, args] = galleryOpenCommand(path);
  await new Promise<void>((resolve, reject) => {
    execFile(command, args, { timeout: 10000, windowsHide: true }, (error) => error ? reject(error) : resolve());
  });
  return "已打开图片图库。";
}
