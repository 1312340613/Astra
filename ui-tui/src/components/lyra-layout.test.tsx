import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import {Writable} from 'node:stream';
import {render} from 'ink';
import stripAnsi from 'strip-ansi';
import stringWidth from 'string-width';
import {ThemeProvider} from '../theme-context.js';
import {THEMES} from '../theme.js';
import {StartupScreen} from './startup-screen.js';
import {WelcomeScreen} from './welcome-screen.js';
class Output extends Writable {isTTY=true;chunks:string[]=[];constructor(public columns:number,public rows:number){super();}_write(d:Buffer,e:unknown,cb:()=>void){this.chunks.push(String(d));cb();}}
const info={skills:15,tools:72,model:'a-very-long-model-name-for-layout',learning:{mode:'review' as const,auto:true,pending:0},mcp:[{name:'local',state:'ready'}]};
for(const [columns,rows] of [[80,24],[80,36],[90,30],[130,40],[130,70]])for(const screen of ['startup','welcome']){
 test(`${screen} Lyra fits and shows artwork at ${columns}x${rows}`,async()=>{
  const out=new Output(columns,rows);
  const element=screen==='startup'?<StartupScreen columns={columns} rows={rows} info={info} animate={false}/>:<WelcomeScreen columns={columns} rows={rows} info={info} model={info.model} sessionName="long-session-name-for-layout"/>;
  const instance=render(<ThemeProvider theme={THEMES.lyra}>{element}</ThemeProvider>,{stdout:out as any,debug:true,patchConsole:false,exitOnCtrlC:false});
  await new Promise(r=>setTimeout(r,15));instance.unmount();const raw=stripAnsi(out.chunks[0]??'').trimEnd();
  if((screen==="startup"&&rows>=30)||rows>=36)assert.match(raw,/[▀▄]/);const lines=raw.split('\n');assert.ok(lines.length<=rows-1,`height ${lines.length}`);
  if(rows===70){const artRows=lines.flatMap((line,index)=>/[▀▄]/.test(line)?[index]:[]);assert.equal(artRows.at(-1)!-artRows[0]+1,48,'large window uses full figure, including background-only rows');}
  assert.ok(lines.every(line=>stringWidth(line)<=columns),'width overflow');assert.match(raw,/MCP/);assert.match(raw,/72/);
 });
}


test('Lyra startup does not continuously repaint the portrait',async()=>{
 const out=new Output(121,31);
 const instance=render(<ThemeProvider theme={THEMES.lyra}><StartupScreen columns={120} rows={31} info={null} animate={true}/></ThemeProvider>,{stdout:out as any,debug:true,patchConsole:false,exitOnCtrlC:false});
 try {
  await new Promise(r=>setTimeout(r,30));
  const frames=()=>out.chunks.filter(chunk=>stripAnsi(chunk).includes('NIGHT TERMINAL')).length;
  const initial=frames();assert.ok(initial>0);
  await new Promise(r=>setTimeout(r,240));
  assert.equal(frames(),initial,'unchanged loading scene should not repaint on a timer');
 }finally{instance.unmount();}
});
