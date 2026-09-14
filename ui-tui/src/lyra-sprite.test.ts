import assert from 'node:assert/strict';
import test from 'node:test';
import { LYRA_PALETTE, LYRA_SPRITE, pixelCell, fitLyraSprite, alignLyraRowsForTerminal } from './lyra-sprite.js';
import avatar32 from '../assets/lyra/v4-draft/avatar-32.json';
import avatar40 from '../assets/lyra/v4-draft/avatar-40.json';
import avatar48 from '../assets/lyra/v4-draft/avatar-48.json';
import previous from './assets/lyra-v3.json';

test('runtime preserves approved portrait colors and original full figure',()=>{
 for(const source of [avatar32,avatar40,avatar48]){
  const size=source.grid.length;
  const rendered=fitLyraSprite(size,size/2);
  assert.equal(rendered[0][0],'.','outer background must be transparent');
  rendered.forEach((row,y)=>row.forEach((key,x)=>{
    const color=source.palette[source.grid[y][x]];
    if(key==='.') assert.equal(color,'#f6f1e7');
    else assert.equal(LYRA_PALETTE[key],color);
  }));
 }
 const full=previous.variants.find(v=>v.id==='full-48')!;
 assert.deepEqual(LYRA_SPRITE.map(row=>row.map(key=>LYRA_PALETTE[key]??null)),
   full.pixels.map(row=>row.map(index=>index<0?null:previous.palette[index])));
});

test('approved full figure is retained at native size', () => {
  assert.equal(LYRA_SPRITE.length,96);
  assert.ok(LYRA_SPRITE.every(row=>row.length===34));
  assert.ok(LYRA_SPRITE.flat().every(key=>key==='.'||key in LYRA_PALETTE));
  assert.ok(LYRA_SPRITE.at(-1)!.some(key=>key!=='.'));
});
test('half cells preserve transparent background on either half',()=>{
 assert.deepEqual(pixelCell('.','.'),{text:' '});
 const key=Object.keys(LYRA_PALETTE)[0];
 assert.deepEqual(pixelCell(key,'.'),{text:'▀',color:LYRA_PALETTE[key]});
 assert.deepEqual(pixelCell('.',key),{text:'▄',color:LYRA_PALETTE[key]});
 assert.deepEqual(pixelCell(key,key),{text:' ',backgroundColor:LYRA_PALETTE[key]});
 const other=Object.keys(LYRA_PALETTE)[1];
 assert.deepEqual(pixelCell(key,other),{text:'▄',color:LYRA_PALETTE[other],backgroundColor:LYRA_PALETTE[key]});
});
test('approved avatar grids are used without runtime resizing',()=>{
 for(const [columns,rows,width,height] of [[32,16,32,32],[40,20,40,40],[48,24,48,48],[34,48,34,96],[100,100,34,96]]){
   const grid=fitLyraSprite(columns,rows);
   assert.equal(grid.length,height,`${columns}x${rows}`);assert.equal(grid[0].length,width);
 }
 for(const [cols,rows] of [[31,20],[40,15],[Infinity,20],[40,NaN],[-1,40]])assert.deepEqual(fitLyraSprite(cols,rows),[]);
});
test('intermediate budgets preserve native pixels and fit both axes',()=>{
 assert.strictEqual(fitLyraSprite(39,19),fitLyraSprite(32,16));
 assert.strictEqual(fitLyraSprite(47,40),fitLyraSprite(40,20));
 for(let cols=32;cols<=60;cols++)for(let rows=16;rows<=55;rows++){
   const grid=fitLyraSprite(cols,rows);
   assert.ok(grid.length<=rows*2&&grid[0].length<=cols);
   assert.ok(grid.flat().every(key=>key==='.'||key in LYRA_PALETTE));
   assert.ok(Object.isFrozen(grid)&&grid.every(Object.isFrozen));
 }
});

test('small portrait alignment only relocates transparent padding',()=>{
 const original=fitLyraSprite(32,16);
 const aligned=alignLyraRowsForTerminal(original);
 assert.ok(original[0].every(pixel=>pixel==='.'),'only blank rows may move');
 assert.equal(aligned.length,original.length);
 assert.deepEqual(aligned.slice(0,-1),original.slice(1));
 assert.deepEqual(aligned.at(-1),original[0]);
 assert.deepEqual(aligned.flat().filter(pixel=>pixel!=='.'),original.flat().filter(pixel=>pixel!=='.'));
 // At the reported left-eye seam, both the red eye and skin immediately below
 // now use full background cells instead of exposing red beneath a skin glyph.
 assert.equal(pixelCell(original[20][11],original[21][11]).text,'▄');
 assert.equal(pixelCell(aligned[18][11],aligned[19][11]).text,' ');
 assert.equal(pixelCell(aligned[20][11],aligned[21][11]).text,' ');
});

test('alignment retains full figure and larger portraits with better original pairing',()=>{
 for(const [columns,rows] of [[34,48],[40,20],[48,24]]){
  const original=fitLyraSprite(columns,rows);
  assert.strictEqual(alignLyraRowsForTerminal(original),original);
 }
 assert.deepEqual(alignLyraRowsForTerminal([]),[]);
});
