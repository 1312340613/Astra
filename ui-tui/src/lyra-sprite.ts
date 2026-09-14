import atlas from './assets/lyra-v4.json';

export type LyraPixels = readonly (readonly string[])[];
const keys = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';
export const LYRA_PALETTE: Readonly<Record<string, string>> = Object.freeze(
  Object.fromEntries(atlas.palette.map((color, index) => [keys[index], color])),
);
export const LYRA_VARIANTS = Object.freeze(atlas.variants.map((variant) => Object.freeze({
  id: variant.id,
  kind: variant.kind,
  columns: variant.columns,
  rows: variant.rows,
  pixels: Object.freeze(variant.pixels.map(row => Object.freeze(row.map(index => index < 0 ? '.' : keys[index])))),
})));
export const LYRA_SPRITE = LYRA_VARIANTS.find(variant => variant.id === 'full-48')!.pixels;
const EMPTY: LyraPixels = Object.freeze([]);

/** Only native, reviewed sizes: never resample a small face at runtime. */
export function selectLyraSprite(columns: number, rows: number) {
  if (!Number.isFinite(columns) || !Number.isFinite(rows)) return undefined;
  return ['full-48', 'avatar-48', 'avatar-40', 'avatar-32']
    .map(id => LYRA_VARIANTS.find(variant => variant.id === id)!)
    .find(variant => variant.columns <= columns && variant.rows <= rows);
}
export function fitLyraSprite(columns: number, rows: number): LyraPixels {
  return selectLyraSprite(columns, rows)?.pixels ?? EMPTY;
}

// Measured for the approved 32-pixel portrait: relocating its empty top row
// reduces contrasting backgrounds exposed beneath lower-block glyphs. Keep
// every painted pixel and the canvas size; larger variants pair better as-is.
const compactPixels = LYRA_VARIANTS.find(variant => variant.id === 'avatar-32')!.pixels;
const alignedCompactPixels: LyraPixels = compactPixels[0].every(pixel => pixel === '.')
  ? Object.freeze([...compactPixels.slice(1), compactPixels[0]])
  : compactPixels;
export function alignLyraRowsForTerminal(sprite: LyraPixels): LyraPixels {
  return sprite === compactPixels ? alignedCompactPixels : sprite;
}

export interface PixelCell { text: string; color?: string; backgroundColor?: string }
export function pixelCell(upper: string, lower: string): PixelCell {
  const top = LYRA_PALETTE[upper], bottom = LYRA_PALETTE[lower];
  if (!top && !bottom) return { text: ' ' };
  if (!top) return { text: '▄', color: bottom };
  if (!bottom) return { text: '▀', color: top };
  // A full background bypasses font metrics when both pixels match. For mixed
  // cells, the lower glyph avoids SF Mono Terminal's large upper-block top gap.
  if (top === bottom) return { text: ' ', backgroundColor: top };
  return { text: '▄', color: bottom, backgroundColor: top };
}
export function lyraSvg(sprite: LyraPixels = LYRA_SPRITE): string {
  const width = sprite[0]?.length ?? 0, height = sprite.length;
  const pixels = sprite.flatMap((row, y) => row.flatMap((key, x) => key === '.' ? [] : [
    `<rect x="${x}" y="${y}" width="1" height="1" fill="${LYRA_PALETTE[key]}"/>`,
  ])).join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width * 8}" height="${height * 8}" shape-rendering="crispEdges"><title>Lyra pixel character</title>${pixels}</svg>`;
}
