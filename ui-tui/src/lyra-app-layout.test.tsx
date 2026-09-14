import assert from "node:assert/strict";
import test from "node:test";
import {mkdtempSync,mkdirSync,writeFileSync,rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { EventEmitter } from "node:events";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stripAnsi from "strip-ansi";

class FakeChild extends EventEmitter {
  stdin = new PassThrough();
  stdout = new PassThrough();
  stderr = new PassThrough();
  commands: any[] = [];
  constructor() { super(); this.stdin.on("data", (data) => this.commands.push(JSON.parse(String(data)))); }
  kill() { return true; }
  event(event: object) { this.stdout.write(JSON.stringify(event) + "\n"); }
}
const children: FakeChild[] = [];
childProcess.spawn = (() => { const child = new FakeChild(); children.push(child); return child; }) as unknown as typeof childProcess.spawn;
syncBuiltinESMExports();
process.env.TUI_STARTUP_ANIMATION = "0";
const settingsRoot=mkdtempSync(join(tmpdir(),"lyra-layout-"));
mkdirSync(join(settingsRoot,".astra"));
writeFileSync(join(settingsRoot,".astra","tui-settings.json"),JSON.stringify({theme:"lyra"}));
process.env.AGENT_PROJECT_ROOT=settingsRoot;
test.after(()=>rmSync(settingsRoot,{recursive:true,force:true}));
const { default: App } = await import("./app.js");
class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
class Output extends Writable {
  columns = 90; rows = 30; isTTY = true; chunks: string[] = [];
  _write(data: Buffer, _: BufferEncoding, done: () => void) { this.chunks.push(String(data)); done(); }
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 60));
async function setup(columns = 90, rows = 30) {
  const stdin = new Input(); const stdout = new Output();
  stdout.columns = columns; stdout.rows = rows;
  const app = render(<App />, { stdin: stdin as any, stdout: stdout as any, stderr: stdout as any, debug: true, patchConsole: false, exitOnCtrlC: false });
  await settle();
  return { app, stdin, stdout, child: children.at(-1)!, frame: () => stdout.chunks.map(stripAnsi).filter((chunk) => chunk.trim()).at(-1) ?? "", async key(value: string) { stdin.write(value); await settle(); }, async submit(value: string) { stdin.write(value); await settle(); stdin.write("\r"); await settle(); } };
}

for(const [columns,rows] of [[121,31],[80,24],[80,32],[90,30],[130,30],[130,32],[130,36],[130,40],[130,70]])test(`full app welcome fits ${columns}x${rows}`,async()=>{
 const h=await setup(columns,rows);try{
 h.child.event({type:"startup_banner",skills:15,tools:72,model:"test-model",learning:{mode:"review",auto:true,pending:0},mcp:[]});
 h.child.event({type:"model_info",model:"test",total_tokens:0,prompt_tokens:0,completion_tokens:0,context_pct:0,context_limit:10000,reasoning_effort:"high"});await settle();
 const frame=h.frame();assert.match(frame,/READY/);assert.match(frame,/MODEL/);assert.match(frame,/CTX/);if(rows>=30)assert.match(frame,/[▀▄]/);const artRows=frame.split("\n").flatMap((line,index)=>/[▀▄]/.test(line)?[index]:[]);assert.ok((artRows.length?artRows.at(-1)!-artRows[0]+1:0)>=(rows===70?48:rows>=30?15:0),"art must not be vertically clipped; aligned compact portrait has 15 painted rows plus one transparent padding row");assert.ok(frame.trimEnd().split("\n").length<=rows,`height ${frame.trimEnd().split("\n").length}`);
 }finally{h.app.unmount();}
});
