import assert from "node:assert/strict";
import { slashCommandSuggestions, completeSlashCommand, resolveSlashCommandSubmission } from "./command-menu.js";
import type { CommandMenuContext } from "./command-menu.js";
const models = [
  { name: "shared", key: "alpha::shared", provider_id: "alpha", provider: "Alpha", source: "live", current: true },
  { name: "shared", key: "beta::shared", provider_id: "beta", provider: "Beta", source: "cache" },
  { name: "fresh", key: "alpha::fresh", provider_id: "alpha", provider: "Alpha", source: "live", metadata_known: false },
];
const context: CommandMenuContext = { providers: [
  { id: "alpha", label: "Alpha", endpoint: "https://a.example/v1", connected: true, source: "live", error: "", count: 2 },
  { id: "beta", label: "Beta", endpoint: "https://b.example/v1", connected: true, source: "cache", error: "", count: 1 },
  { id: "no-key", label: "Unconnected", endpoint: "https://c.example/v1", connected: false, source: "preset", error: "", count: 0 },
], recentModels: ["beta::shared", "removed::gone"] };
const root = slashCommandSuggestions("/model ", [], models, context);
assert.deepEqual(root.map(m => m.command), ["Alpha", "Beta", "shared", "Connect a provider…"]);
assert.equal(root[0].kind, "submenu");
assert.equal(completeSlashCommand("/model ", 0, [], models, context), "/model alpha::");
assert.equal(resolveSlashCommandSubmission("/model ", 0, [], models, context).kind, "none");
const subset = slashCommandSuggestions("/model alpha::", [], models, context);
assert.equal(subset.filter(m => m.command === "shared").length, 1);
assert.equal(subset[0].submitValue, "/model alpha::shared");
assert.equal(subset.at(-1)?.completion, "/model ");
assert.equal(subset.at(-2)?.submitValue, "/model-refresh alpha");
assert.match(subset[1].description, /capabilities unknown/);
assert.equal(resolveSlashCommandSubmission("/model alpha::future/model", 0, [], models, context).kind, "submit");
assert.equal(slashCommandSuggestions("/model alpha::future/model", [], models, context)[0].submitValue, "/model alpha::future/model");
assert.equal(slashCommandSuggestions("/model fresh", [], models, context)[0].submitValue, "/model alpha::fresh");
assert.equal(slashCommandSuggestions("/model missing::", [], [], { providers: [], recentModels: [] }).at(-1)?.completion, "/model ");
