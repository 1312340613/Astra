import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import { InputBar } from "./input-bar.js";
import { ThemeProvider } from "../theme-context.js";
import { THEMES } from "../theme.js";
class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
class Output extends Writable {
  columns = 100; rows = 30; isTTY = true;
  _write(_: Buffer, _encoding: BufferEncoding, cb: (e?: Error | null) => void) { cb(); }
}
const input = new Input(), output = new Output();
const requests: (string | undefined)[] = [], submissions: string[] = [];
const refresh = (id?: string) => requests.push(id);
const frame = (names: string[]) => <ThemeProvider theme={THEMES.glitchcity}>
  <InputBar disabled={false} sessionList={[]} modelList={names.map(name => ({ name, key: `alpha::${name}`, provider_id: "alpha" }))}
    providers={[{ id: "alpha", label: "Alpha", endpoint: "http://localhost:8999/v1", connected: true, source: "live", error: "", count: names.length }]}
    onSubmit={s => submissions.push(s.text)} onModelMenuOpen={refresh} menuRows={8} />
</ThemeProvider>;
const app = render(frame(["a"]), { stdin: input as unknown as NodeJS.ReadStream, stdout: output as unknown as NodeJS.WriteStream,
  stderr: output as unknown as NodeJS.WriteStream, debug: true, patchConsole: false, exitOnCtrlC: false });
const settle = () => new Promise(r => setTimeout(r, 45));
const press = async (s: string) => { input.write(s); await settle(); };
await settle();
await press("/model");
await press("\r");
assert.deepEqual(requests, [undefined, "alpha"]);
await press("\u001b[B");
await press("\u001b[B"); // Refresh, after a and manual entry.
app.rerender(frame(["a", "b"]));
await settle();
await press("\r");
assert.deepEqual(submissions, ["/model-refresh alpha"]);
app.unmount();
