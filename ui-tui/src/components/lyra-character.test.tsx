import assert from 'node:assert/strict';
import test from 'node:test';
import {Writable} from 'node:stream';
import React from 'react';
import {render} from 'ink';
import stripAnsi from 'strip-ansi';
import stringWidth from 'string-width';
import {LyraCharacter} from './lyra-character.js';
class Output extends Writable {
  columns=80; rows=40; isTTY=true; chunks:string[]=[];
  _write(data:Buffer,_encoding:BufferEncoding,done:()=>void){this.chunks.push(String(data));done();}
}
test('actual Ink character fits both axes and emits half blocks with color',async()=>{
  const out=new Output();
  const instance=render(<LyraCharacter columns={32} rows={20}/>,{stdout:out as unknown as NodeJS.WriteStream,debug:true,patchConsole:false,exitOnCtrlC:false});
  await new Promise(resolve=>setTimeout(resolve,10));instance.unmount();
  const raw=out.chunks[0]??'';const lines=stripAnsi(raw).trimEnd().split('\n');
  assert.ok(lines.length<=20);assert.ok(lines.every(line=>stringWidth(line)<=32));
  assert.match(raw,/[▀▄]/u);
});
