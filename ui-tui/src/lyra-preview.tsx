/** Offline review of the exact runtime grids. No backend or image dependencies. */
import {writeFileSync, mkdirSync} from 'node:fs';
import {resolve} from 'node:path';
import {LYRA_VARIANTS, lyraSvg} from './lyra-sprite.js';
const output = resolve(process.argv[2] ?? '/tmp/astra-lyra-preview');
mkdirSync(output, {recursive: true});
for (const variant of LYRA_VARIANTS) {
  writeFileSync(resolve(output, `${variant.id}.svg`), lyraSvg(variant.pixels));
}
const cards = LYRA_VARIANTS.map(variant => `<section><h2>${variant.kind === 'bust' ? '半身近照' : '全身像'} · ${variant.columns} 列 × ${variant.rows} 行</h2><div class="art">${lyraSvg(variant.pixels)}</div><p>${variant.kind === 'full' && variant.rows < 48 ? '保留的对照资产，自动布局不使用此尺寸。' : '界面可按空间预算选用的原生尺寸。'}</p><a href="${variant.id}.svg" download>保存 SVG</a></section>`).join('');
writeFileSync(resolve(output, 'index.html'), `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lyra · 像素资产</title><style>*{box-sizing:border-box}body{margin:0;background:#11131b;color:#ebe9e4;font:16px/1.6 system-ui}main{max-width:1400px;margin:auto;padding:36px}h1{margin:0}p{color:#a5a7b5}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}section{background:#1e222c;border:1px solid #343746;border-radius:12px;padding:24px}h2{font-size:17px}.art{min-height:330px;display:flex;justify-content:center;align-items:flex-start;overflow:auto}svg{flex-shrink:0;image-rendering:pixelated}a{color:#eeaaa8}button{background:#343746;color:inherit;border:1px solid #777;padding:8px 16px;margin-bottom:24px;border-radius:6px}.light section{background:#ede9df}.light section h2,.light section p{color:#292d39}</style><main><h1>Lyra · 全身与半身像素资产</h1><p>原生像素网格与 Ink 半块字符共用数据。此页是像素预览，不是真实终端截图。</p><button onclick="document.body.classList.toggle('light')">切换背景</button><div class="grid">${cards}</div></main></html>`);
process.stdout.write(`${resolve(output, 'index.html')}\n`);
