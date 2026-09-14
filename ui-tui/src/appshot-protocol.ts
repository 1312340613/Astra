// Strict Appshot v1 wire contract. Parsed values are not artifact authority.
export const AppshotLimits = {
  protocolVersion: 1,
  maximumFrameBytes: 65536,
  maximumAppshotsPerDraft: 4,
  maximumPNGBytes: 10485760,
  maximumAXBytes: 262144,
} as const;
export class AppshotValidationError extends Error {}
export interface AppshotBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface AppshotSource {
  pid: number;
  process_start: string;
  bundle_id: string;
  app_label: string;
  window_title: string;
  window_id: number;
  bounds: AppshotBounds;
}
export interface AppshotPNG {
  name: string;
  size: number;
  width: number;
  height: number;
  sha256: string;
  device: string;
  inode: string;
  owner: number;
  mode: number;
  link_count: number;
}
export interface AppshotAX {
  name: string;
  size: number;
  sha256: string;
  device: string;
  inode: string;
  owner: number;
  mode: number;
  link_count: number;
  coverage: "reported_ax_subtree" | "unavailable";
  node_count: number;
  depth: number;
  truncated: boolean;
  truncation_reasons: string[];
}
export interface AppshotBrokerBinding {
  instance_id: string;
  session_id: string;
  process_start: string;
}
export interface AppshotManifest {
  schema_version: number;
  token: string;
  captured_at: string;
  source: AppshotSource;
  png: AppshotPNG;
  ax: AppshotAX;
  broker: AppshotBrokerBinding;
}
export interface AppshotHello {
  type: "hello";
  version: number;
  session_id: string;
  pid: number;
  process_start: string;
  client_nonce: string;
}
export interface AppshotHelloAck {
  type: "hello_ack";
  version: number;
  instance_id: string;
  broker_nonce: string;
  session_id: string;
}
export interface AppshotClientState {
  type: "client_state";
  version: number;
  request_id: string;
  broker_id: string;
  session_id: string;
  activity_ns: string;
  appshot_count: number;
  can_accept: boolean;
}
export interface AppshotAttachOffer {
  type: "attach_offer";
  version: number;
  request_id: string;
  broker_id: string;
  session_id: string;
  manifest_path: string;
}
export interface AppshotAttachAck {
  type: "attach_ack";
  version: number;
  request_id: string;
  broker_id: string;
  session_id: string;
  accepted: boolean;
  reason: string;
}
export interface AppshotAttachCommit {
  type: "attach_commit";
  version: number;
  request_id: string;
  broker_id: string;
  session_id: string;
  manifest_path: string;
}
export interface AppshotAttachRevoke {
  type: "attach_revoke";
  version: number;
  request_id: string;
  broker_id: string;
  session_id: string;
  reason: string;
}
export interface AppshotRelease {
  type: "release";
  version: number;
  request_id: string;
  broker_id: string;
  session_id: string;
}
export interface AppshotReleaseAck {
  type: "release_ack";
  version: number;
  request_id: string;
  broker_id: string;
  session_id: string;
  released: boolean;
}
export interface AppshotCommand {
  type: "command";
  version: number;
  request_id: string;
  broker_id: string;
  session_id: string;
  name: "status" | "enable" | "disable" | "shortcut";
  argument: string;
}
export interface AppshotCommandResult {
  type: "command_result";
  version: number;
  request_id: string;
  broker_id: string;
  session_id: string;
  ok: boolean;
  code: string;
  message: string;
}
export interface AppshotClientStateAck {
  type: "client_state_ack";
  version: number;
  request_id: string;
  broker_id: string;
  session_id: string;
  appshot_count: number;
  can_accept: boolean;
}
export interface AppshotStatus {
  type: "status";
  version: number;
  request_id: string;
  broker_id: string;
  session_id: string;
  enabled: boolean;
  chord: string;
  registration: "registered" | "conflict" | "unavailable";
  connected_tuis: number;
  permission: "ready" | "unavailable" | "unknown";
  appshot_count: number;
  can_accept: boolean;
}
export type AppshotClientMessage =
  | AppshotHello
  | AppshotClientState
  | AppshotAttachAck
  | AppshotRelease
  | AppshotCommand;
export type AppshotBrokerMessage =
  | AppshotHelloAck
  | AppshotAttachOffer
  | AppshotAttachCommit
  | AppshotAttachRevoke
  | AppshotReleaseAck
  | AppshotCommandResult
  | AppshotClientStateAck
  | AppshotStatus;
export type AppshotMessage = AppshotClientMessage | AppshotBrokerMessage;
function fail(code: string): never {
  throw new AppshotValidationError(code);
}
function decode(raw: string | Uint8Array): unknown {
  const bytes = typeof raw === "string" ? Buffer.from(raw, "utf8") : raw;
  if (!bytes.length || bytes.length > 65536) fail("frame_size");
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    fail("invalid_utf8");
  }
  let i = 0;
  const ws = () => {
    while (/[ \r\n\t]/.test(text[i] ?? "!")) i++;
  };
  const string = (): string => {
    const start = i++;
    while (i < text.length) {
      const c = text[i++];
      if (c === '"') {
        try {
          const parsed: string = JSON.parse(text.slice(start, i));
          if (
            [...parsed].some((c) => {
              const n = c.codePointAt(0)!;
              return n >= 0xd800 && n <= 0xdfff;
            })
          )
            fail("invalid_unicode");
          return parsed;
        } catch {
          fail("invalid_json");
        }
      }
      if (c === "\\") i++;
    }
    return fail("invalid_json");
  };
  const value = (depth: number): unknown => {
    if (depth > 32) fail("json_depth");
    ws();
    const c = text[i];
    if (c === '"') return string();
    if (c === "{") {
      i++;
      ws();
      const obj: Record<string, unknown> = Object.create(null);
      if (text[i] === "}") {
        i++;
        return obj;
      }
      while (true) {
        ws();
        if (text[i] !== '"') fail("invalid_json");
        const k = string();
        if (Object.hasOwn(obj, k)) fail("duplicate_key");
        ws();
        if (text[i++] !== ":") fail("invalid_json");
        obj[k] = value(depth + 1);
        ws();
        const end = text[i++];
        if (end === "}") return obj;
        if (end !== ",") fail("invalid_json");
      }
    }
    if (c === "[") {
      i++;
      ws();
      const a: unknown[] = [];
      if (text[i] === "]") {
        i++;
        return a;
      }
      while (true) {
        a.push(value(depth + 1));
        ws();
        const end = text[i++];
        if (end === "]") return a;
        if (end !== ",") fail("invalid_json");
      }
    }
    const match =
      /^(?:true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)/.exec(
        text.slice(i),
      );
    if (!match) fail("invalid_json");
    i += match[0].length;
    const v = JSON.parse(match[0]);
    if (typeof v === "number" && !Number.isFinite(v)) fail("nonfinite");
    return v;
  };
  const v = value(0);
  ws();
  if (i !== text.length) fail("invalid_json");
  return v;
}
function object(v: unknown, keys: string[]): Record<string, unknown> {
  if (!v || typeof v !== "object" || Array.isArray(v)) fail("invalid_fields");
  const o = v as Record<string, unknown>;
  if (
    Object.keys(o).length !== keys.length ||
    keys.some((k) => !Object.hasOwn(o, k))
  )
    fail("invalid_fields");
  return o;
}
function rule(v: unknown, r: readonly (string | number)[]): any {
  const k = r[0];
  let valid = false;
  if (k === "int")
    valid =
      typeof v === "number" &&
      Number.isSafeInteger(v) &&
      v >= Number(r[1]) &&
      v <= Number(r[2]);
  else if (k === "coord")
    valid =
      typeof v === "number" && Number.isFinite(v) && Math.abs(v) <= 1000000;
  else if (k === "extent")
    valid =
      typeof v === "number" && Number.isFinite(v) && v > 0 && v <= 1000000;
  else if (k === "bool") valid = typeof v === "boolean";
  else if (k === "reasons") {
    if (!Array.isArray(v) || v.length > 16) fail("invalid_reasons");
    return v.map((x) => rule(x, ["str", 128]));
  } else if (typeof v === "string") {
    if (k === "str")
      valid = Buffer.byteLength(v) <= Number(r[1]) && !v.includes("\0");
    else if (k === "enum") valid = r.slice(1).includes(v);
    else if (k === "uint")
      valid =
        /^(0|[1-9][0-9]{0,19})$/.test(v) && BigInt(v) <= 18446744073709551615n;
    else if (k === "hash") valid = /^[0-9a-f]{64}$/.test(v);
    else if (k === "token") valid = /^[0-9a-f]{32}$/.test(v);
    else if (k === "id") valid = /^[A-Za-z0-9_-]{1,128}$/.test(v);
    else if (k === "path")
      valid =
        v.startsWith("/") &&
        Buffer.byteLength(v) <= 4096 &&
        !v.includes("\0") &&
        !v.split("/").some((x) => x === "." || x === "..");
    else if (k === "date")
      valid = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/.test(
        v,
      );
  }
  if (
    typeof v === "string" &&
    ["uint", "hash", "token", "id", "date"].includes(String(k)) &&
    /[\r\n]/.test(v)
  )
    valid = false;
  if (k === "date" && valid && typeof v === "string") {
    const date = new Date(v);
    valid =
      Number(v.slice(0, 4)) >= 1 &&
      Number.isFinite(date.getTime()) &&
      date.toISOString() === v.replace("Z", ".000Z");
  }
  if (!valid) fail("invalid_" + k);
  return v;
}
function makeAppshotBounds(v: unknown): AppshotBounds {
  const o = object(v, ["x", "y", "width", "height"]);
  return {
    x: rule(o.x, ["coord"]),
    y: rule(o.y, ["coord"]),
    width: rule(o.width, ["extent"]),
    height: rule(o.height, ["extent"]),
  };
}
function makeAppshotSource(v: unknown): AppshotSource {
  const o = object(v, [
    "pid",
    "process_start",
    "bundle_id",
    "app_label",
    "window_title",
    "window_id",
    "bounds",
  ]);
  return {
    pid: rule(o.pid, ["int", 1, 2147483647]),
    process_start: rule(o.process_start, ["uint"]),
    bundle_id: rule(o.bundle_id, ["str", 256]),
    app_label: rule(o.app_label, ["str", 256]),
    window_title: rule(o.window_title, ["str", 1024]),
    window_id: rule(o.window_id, ["int", 0, 4294967295]),
    bounds: makeAppshotBounds(o.bounds),
  };
}
function makeAppshotPNG(v: unknown): AppshotPNG {
  const o = object(v, [
    "name",
    "size",
    "width",
    "height",
    "sha256",
    "device",
    "inode",
    "owner",
    "mode",
    "link_count",
  ]);
  return {
    name: rule(o.name, ["str", 128]),
    size: rule(o.size, ["int", 1, 10485760]),
    width: rule(o.width, ["int", 1, 16384]),
    height: rule(o.height, ["int", 1, 16384]),
    sha256: rule(o.sha256, ["hash"]),
    device: rule(o.device, ["uint"]),
    inode: rule(o.inode, ["uint"]),
    owner: rule(o.owner, ["int", 0, 4294967295]),
    mode: rule(o.mode, ["int", 384, 384]),
    link_count: rule(o.link_count, ["int", 1, 1]),
  };
}
function makeAppshotAX(v: unknown): AppshotAX {
  const o = object(v, [
    "name",
    "size",
    "sha256",
    "device",
    "inode",
    "owner",
    "mode",
    "link_count",
    "coverage",
    "node_count",
    "depth",
    "truncated",
    "truncation_reasons",
  ]);
  const result: AppshotAX = {
    name: rule(o.name, ["str", 128]),
    size: rule(o.size, ["int", 1, 262144]),
    sha256: rule(o.sha256, ["hash"]),
    device: rule(o.device, ["uint"]),
    inode: rule(o.inode, ["uint"]),
    owner: rule(o.owner, ["int", 0, 4294967295]),
    mode: rule(o.mode, ["int", 384, 384]),
    link_count: rule(o.link_count, ["int", 1, 1]),
    coverage: rule(o.coverage, ["enum", "reported_ax_subtree", "unavailable"]),
    node_count: rule(o.node_count, ["int", 0, 2000]),
    depth: rule(o.depth, ["int", 0, 64]),
    truncated: rule(o.truncated, ["bool"]),
    truncation_reasons: rule(o.truncation_reasons, ["reasons"]),
  };
  if (result.coverage === "unavailable" && (
    result.node_count !== 0 || result.depth !== 0 || !result.truncated || !result.truncation_reasons.length
  )) fail("invalid_ax");
  return result;
}
function makeAppshotBrokerBinding(v: unknown): AppshotBrokerBinding {
  const o = object(v, ["instance_id", "session_id", "process_start"]);
  return {
    instance_id: rule(o.instance_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
    process_start: rule(o.process_start, ["uint"]),
  };
}
function makeAppshotManifest(v: unknown): AppshotManifest {
  const o = object(v, [
    "schema_version",
    "token",
    "captured_at",
    "source",
    "png",
    "ax",
    "broker",
  ]);
  return {
    schema_version: rule(o.schema_version, ["int", 1, 1]),
    token: rule(o.token, ["token"]),
    captured_at: rule(o.captured_at, ["date"]),
    source: makeAppshotSource(o.source),
    png: makeAppshotPNG(o.png),
    ax: makeAppshotAX(o.ax),
    broker: makeAppshotBrokerBinding(o.broker),
  };
}
function makeAppshotHello(v: unknown): AppshotHello {
  const o = object(v, [
    "type",
    "version",
    "session_id",
    "pid",
    "process_start",
    "client_nonce",
  ]);
  return {
    type: rule(o.type, ["enum", "hello"]),
    version: rule(o.version, ["int", 1, 1]),
    session_id: rule(o.session_id, ["id"]),
    pid: rule(o.pid, ["int", 1, 2147483647]),
    process_start: rule(o.process_start, ["uint"]),
    client_nonce: rule(o.client_nonce, ["id"]),
  };
}
function makeAppshotHelloAck(v: unknown): AppshotHelloAck {
  const o = object(v, [
    "type",
    "version",
    "instance_id",
    "broker_nonce",
    "session_id",
  ]);
  return {
    type: rule(o.type, ["enum", "hello_ack"]),
    version: rule(o.version, ["int", 1, 1]),
    instance_id: rule(o.instance_id, ["id"]),
    broker_nonce: rule(o.broker_nonce, ["id"]),
    session_id: rule(o.session_id, ["id"]),
  };
}
function makeAppshotClientState(v: unknown): AppshotClientState {
  const o = object(v, [
    "type",
    "version",
    "request_id",
    "broker_id",
    "session_id",
    "activity_ns",
    "appshot_count",
    "can_accept",
  ]);
  return {
    type: rule(o.type, ["enum", "client_state"]),
    version: rule(o.version, ["int", 1, 1]),
    request_id: rule(o.request_id, ["id"]),
    broker_id: rule(o.broker_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
    activity_ns: rule(o.activity_ns, ["uint"]),
    appshot_count: rule(o.appshot_count, ["int", 0, 4]),
    can_accept: rule(o.can_accept, ["bool"]),
  };
}
function makeAppshotAttachOffer(v: unknown): AppshotAttachOffer {
  const o = object(v, [
    "type",
    "version",
    "request_id",
    "broker_id",
    "session_id",
    "manifest_path",
  ]);
  return {
    type: rule(o.type, ["enum", "attach_offer"]),
    version: rule(o.version, ["int", 1, 1]),
    request_id: rule(o.request_id, ["id"]),
    broker_id: rule(o.broker_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
    manifest_path: rule(o.manifest_path, ["path"]),
  };
}
function makeAppshotAttachAck(v: unknown): AppshotAttachAck {
  const o = object(v, [
    "type",
    "version",
    "request_id",
    "broker_id",
    "session_id",
    "accepted",
    "reason",
  ]);
  return {
    type: rule(o.type, ["enum", "attach_ack"]),
    version: rule(o.version, ["int", 1, 1]),
    request_id: rule(o.request_id, ["id"]),
    broker_id: rule(o.broker_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
    accepted: rule(o.accepted, ["bool"]),
    reason: rule(o.reason, ["str", 256]),
  };
}
function makeAppshotAttachCommit(v: unknown): AppshotAttachCommit {
  const o = object(v, [
    "type",
    "version",
    "request_id",
    "broker_id",
    "session_id",
    "manifest_path",
  ]);
  return {
    type: rule(o.type, ["enum", "attach_commit"]),
    version: rule(o.version, ["int", 1, 1]),
    request_id: rule(o.request_id, ["id"]),
    broker_id: rule(o.broker_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
    manifest_path: rule(o.manifest_path, ["path"]),
  };
}
function makeAppshotAttachRevoke(v: unknown): AppshotAttachRevoke {
  const o = object(v, [
    "type",
    "version",
    "request_id",
    "broker_id",
    "session_id",
    "reason",
  ]);
  return {
    type: rule(o.type, ["enum", "attach_revoke"]),
    version: rule(o.version, ["int", 1, 1]),
    request_id: rule(o.request_id, ["id"]),
    broker_id: rule(o.broker_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
    reason: rule(o.reason, ["str", 256]),
  };
}
function makeAppshotRelease(v: unknown): AppshotRelease {
  const o = object(v, [
    "type",
    "version",
    "request_id",
    "broker_id",
    "session_id",
  ]);
  return {
    type: rule(o.type, ["enum", "release"]),
    version: rule(o.version, ["int", 1, 1]),
    request_id: rule(o.request_id, ["id"]),
    broker_id: rule(o.broker_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
  };
}
function makeAppshotReleaseAck(v: unknown): AppshotReleaseAck {
  const o = object(v, [
    "type",
    "version",
    "request_id",
    "broker_id",
    "session_id",
    "released",
  ]);
  return {
    type: rule(o.type, ["enum", "release_ack"]),
    version: rule(o.version, ["int", 1, 1]),
    request_id: rule(o.request_id, ["id"]),
    broker_id: rule(o.broker_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
    released: rule(o.released, ["bool"]),
  };
}
function makeAppshotCommand(v: unknown): AppshotCommand {
  const o = object(v, [
    "type",
    "version",
    "request_id",
    "broker_id",
    "session_id",
    "name",
    "argument",
  ]);
  return {
    type: rule(o.type, ["enum", "command"]),
    version: rule(o.version, ["int", 1, 1]),
    request_id: rule(o.request_id, ["id"]),
    broker_id: rule(o.broker_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
    name: rule(o.name, ["enum", "status", "enable", "disable", "shortcut"]),
    argument: rule(o.argument, ["str", 256]),
  };
}
function makeAppshotCommandResult(v: unknown): AppshotCommandResult {
  const o = object(v, [
    "type",
    "version",
    "request_id",
    "broker_id",
    "session_id",
    "ok",
    "code",
    "message",
  ]);
  return {
    type: rule(o.type, ["enum", "command_result"]),
    version: rule(o.version, ["int", 1, 1]),
    request_id: rule(o.request_id, ["id"]),
    broker_id: rule(o.broker_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
    ok: rule(o.ok, ["bool"]),
    code: rule(o.code, ["id"]),
    message: rule(o.message, ["str", 1024]),
  };
}
function makeAppshotClientStateAck(v: unknown): AppshotClientStateAck {
  const o = object(v, [
    "type",
    "version",
    "request_id",
    "broker_id",
    "session_id",
    "appshot_count",
    "can_accept",
  ]);
  return {
    type: rule(o.type, ["enum", "client_state_ack"]),
    version: rule(o.version, ["int", 1, 1]),
    request_id: rule(o.request_id, ["id"]),
    broker_id: rule(o.broker_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
    appshot_count: rule(o.appshot_count, ["int", 0, 4]),
    can_accept: rule(o.can_accept, ["bool"]),
  };
}
function makeAppshotStatus(v: unknown): AppshotStatus {
  const o = object(v, [
    "type",
    "version",
    "request_id",
    "broker_id",
    "session_id",
    "enabled",
    "chord",
    "registration",
    "connected_tuis",
    "permission",
    "appshot_count",
    "can_accept",
  ]);
  return {
    type: rule(o.type, ["enum", "status"]),
    version: rule(o.version, ["int", 1, 1]),
    request_id: rule(o.request_id, ["id"]),
    broker_id: rule(o.broker_id, ["id"]),
    session_id: rule(o.session_id, ["id"]),
    enabled: rule(o.enabled, ["bool"]),
    chord: rule(o.chord, ["str", 128]),
    registration: rule(o.registration, [
      "enum",
      "registered",
      "conflict",
      "unavailable",
    ]),
    connected_tuis: rule(o.connected_tuis, ["int", 0, 16]),
    permission: rule(o.permission, ["enum", "ready", "unavailable", "unknown"]),
    appshot_count: rule(o.appshot_count, ["int", 0, 4]),
    can_accept: rule(o.can_accept, ["bool"]),
  };
}
export function parseAppshotManifest(
  raw: string | Uint8Array,
): AppshotManifest {
  const v = decode(raw);
  if (
    v &&
    typeof v === "object" &&
    "schema_version" in v &&
    v.schema_version !== 1
  )
    fail("unsupported_schema");
  const m = makeAppshotManifest(v);
  if (
    m.png.name !== `appshot-${m.token}.png` ||
    m.ax.name !== `appshot-${m.token}.ax.json`
  )
    fail("invalid_name");
  if (m.png.width * m.png.height > 32000000) fail("pixel_limit");
  return m;
}
export function parseAppshotMessage(raw: string | Uint8Array): AppshotMessage {
  const v = decode(raw);
  if (!v || typeof v !== "object" || !("type" in v)) fail("unknown_message");
  switch (v.type) {
    case "hello":
      return makeAppshotHello(v);
    case "hello_ack":
      return makeAppshotHelloAck(v);
    case "client_state":
      return makeAppshotClientState(v);
    case "attach_offer":
      return makeAppshotAttachOffer(v);
    case "attach_ack":
      return makeAppshotAttachAck(v);
    case "attach_commit":
      return makeAppshotAttachCommit(v);
    case "attach_revoke":
      return makeAppshotAttachRevoke(v);
    case "release":
      return makeAppshotRelease(v);
    case "release_ack":
      return makeAppshotReleaseAck(v);
    case "command":
      return makeAppshotCommand(v);
    case "command_result":
      return makeAppshotCommandResult(v);
    case "client_state_ack":
      return makeAppshotClientStateAck(v);
    case "status":
      return makeAppshotStatus(v);
    default:
      return fail("unknown_message");
  }
}
export function parseAppshotClientMessage(
  raw: string | Uint8Array,
): AppshotClientMessage {
  const v = parseAppshotMessage(raw);
  switch (v.type) {
    case "hello":
    case "client_state":
    case "attach_ack":
    case "release":
    case "command":
      return v;
    default:
      return fail("wrong_direction");
  }
}
export function parseAppshotBrokerMessage(
  raw: string | Uint8Array,
): AppshotBrokerMessage {
  const v = parseAppshotMessage(raw);
  switch (v.type) {
    case "hello_ack":
    case "attach_offer":
    case "attach_commit":
    case "attach_revoke":
    case "release_ack":
    case "command_result":
    case "client_state_ack":
    case "status":
      return v;
    default:
      return fail("wrong_direction");
  }
}
export function encodeAppshotMessage(message: AppshotMessage): Buffer {
  const raw = JSON.stringify(message);
  parseAppshotMessage(raw);
  if (Buffer.byteLength(raw) + 1 > 65536) fail("frame_size");
  return Buffer.from(raw + "\n");
}
export class AppshotFrameDecoder {
  private buffer: number[] = [];
  feed(chunk: Uint8Array): AppshotMessage[] {
    const result: AppshotMessage[] = [];
    for (const byte of chunk) {
      if (this.buffer.length + 1 > 65536) {
        this.buffer = [];
        fail("frame_size");
      }
      if (byte === 10) {
        const raw = Buffer.from(this.buffer);
        this.buffer = [];
        result.push(parseAppshotMessage(raw));
      } else this.buffer.push(byte);
    }
    return result;
  }
  finish(): void {
    if (this.buffer.length) {
      this.buffer = [];
      fail("incomplete_frame");
    }
  }
}
// Shared strict JSON and primitive checks; v1 entry points remain v1-only.
export { decode as decodeAppshotJSON, object as appshotObject, rule as appshotRule };
