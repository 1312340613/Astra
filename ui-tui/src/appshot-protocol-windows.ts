// Explicit Windows v2 contract. Validation alone never grants artifact authority.
import { AppshotValidationError, decodeAppshotJSON, appshotObject, appshotRule } from "./appshot-protocol.js";
import type { AppshotMessage, AppshotClientMessage, AppshotBrokerMessage } from "./appshot-protocol.js";

type Rule = readonly (string | number)[] | string;
type Fields = Record<string, Rule>;
const models: Record<string, Fields> = {
  "WindowsProcess": {
    "pid": ["int",1,2147483647],
    "process_start": ["uint"],
    "user_sid": ["sid"],
  },
  "WindowsSource": {
    "process": "WindowsProcess",
    "app_id": ["str",256],
    "app_label": ["str",256],
    "window_title": ["str",1024],
    "window_handle": ["uint"],
    "bounds": "AppshotBounds",
  },
  "WindowsFile": {
    "volume_serial": ["uint"],
    "file_id": ["token"],
    "owner_sid": ["sid"],
    "link_count": ["int",1,1],
  },
  "WindowsPNG": {
    "name": ["str",128],
    "size": ["int",1,10485760],
    "width": ["int",1,16384],
    "height": ["int",1,16384],
    "sha256": ["hash"],
    "identity": "WindowsFile",
  },
  "WindowsUIA": {
    "name": ["str",128],
    "size": ["int",1,262144],
    "sha256": ["hash"],
    "identity": "WindowsFile",
    "coverage": ["enum","reported_uia_subtree","unavailable"],
    "node_count": ["int",0,2000],
    "depth": ["int",0,64],
    "truncated": ["bool"],
    "truncation_reasons": ["reasons"],
  },
  "WindowsBroker": {
    "instance_id": ["id"],
    "session_id": ["id"],
    "recipient": "WindowsProcess",
  },
  "WindowsManifest": {
    "schema_version": ["int",2,2],
    "platform": ["enum","windows"],
    "token": ["token"],
    "captured_at": ["date"],
    "source": "WindowsSource",
    "png": "WindowsPNG",
    "uia": "WindowsUIA",
    "broker": "WindowsBroker",
  },
  "AppshotBounds": {
    "x": ["coord"],
    "y": ["coord"],
    "width": ["extent"],
    "height": ["extent"],
  },
};
const messages: Record<string, Fields> = {
  "hello": {
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "type": ["enum","hello"],
    "session_id": ["id"],
    "pid": ["int",1,2147483647],
    "process_start": ["uint"],
    "user_sid": ["sid"],
    "client_nonce": ["id"],
  },
  "hello_ack": {
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "type": ["enum","hello_ack"],
    "instance_id": ["id"],
    "broker_nonce": ["id"],
    "session_id": ["id"],
  },
  "client_state": {
    "type": ["enum","client_state"],
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "request_id": ["id"],
    "broker_id": ["id"],
    "session_id": ["id"],
    "activity_ns": ["uint"],
    "appshot_count": ["int",0,4],
    "can_accept": ["bool"],
  },
  "attach_offer": {
    "type": ["enum","attach_offer"],
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "request_id": ["id"],
    "broker_id": ["id"],
    "session_id": ["id"],
    "manifest_path": ["windowsPath"],
  },
  "attach_ack": {
    "type": ["enum","attach_ack"],
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "request_id": ["id"],
    "broker_id": ["id"],
    "session_id": ["id"],
    "accepted": ["bool"],
    "reason": ["str",256],
  },
  "attach_commit": {
    "type": ["enum","attach_commit"],
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "request_id": ["id"],
    "broker_id": ["id"],
    "session_id": ["id"],
    "manifest_path": ["windowsPath"],
  },
  "attach_revoke": {
    "type": ["enum","attach_revoke"],
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "request_id": ["id"],
    "broker_id": ["id"],
    "session_id": ["id"],
    "reason": ["str",256],
  },
  "release": {
    "type": ["enum","release"],
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "request_id": ["id"],
    "broker_id": ["id"],
    "session_id": ["id"],
  },
  "release_ack": {
    "type": ["enum","release_ack"],
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "request_id": ["id"],
    "broker_id": ["id"],
    "session_id": ["id"],
    "released": ["bool"],
  },
  "command": {
    "type": ["enum","command"],
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "request_id": ["id"],
    "broker_id": ["id"],
    "session_id": ["id"],
    "name": ["enum","status","enable","disable","shortcut"],
    "argument": ["str",256],
  },
  "command_result": {
    "type": ["enum","command_result"],
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "request_id": ["id"],
    "broker_id": ["id"],
    "session_id": ["id"],
    "ok": ["bool"],
    "code": ["id"],
    "message": ["str",1024],
  },
  "client_state_ack": {
    "type": ["enum","client_state_ack"],
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "request_id": ["id"],
    "broker_id": ["id"],
    "session_id": ["id"],
    "appshot_count": ["int",0,4],
    "can_accept": ["bool"],
  },
  "status": {
    "type": ["enum","status"],
    "version": ["int",2,2],
    "platform": ["enum","windows"],
    "request_id": ["id"],
    "broker_id": ["id"],
    "session_id": ["id"],
    "enabled": ["bool"],
    "chord": ["str",128],
    "registration": ["enum","registered","conflict","unavailable"],
    "connected_tuis": ["int",0,16],
    "permission": ["enum","ready","unavailable","unknown"],
    "appshot_count": ["int",0,4],
    "can_accept": ["bool"],
  },
};

export interface WindowsAppshotProcess { pid: number; process_start: string; user_sid: string }
type WindowsWire<T> = T extends { type: "hello" }
  ? Omit<T, "version"> & { version: 2; platform: "windows"; user_sid: string }
  : T extends unknown ? Omit<T, "version"> & { version: 2; platform: "windows" } : never;
export type WindowsAppshotMessage = WindowsWire<AppshotMessage>;
export type WindowsAppshotClientMessage = WindowsWire<AppshotClientMessage>;
export type WindowsAppshotBrokerMessage = WindowsWire<AppshotBrokerMessage>;
export type WindowsAppshotBinding = WindowsAppshotManifest["broker"];
export interface WindowsAppshotFileIdentity {
  volume_serial: string; file_id: string; owner_sid: string; link_count: number;
}
export interface WindowsAppshotManifest {
  schema_version: 2; platform: "windows"; token: string; captured_at: string;
  source: {
    process: WindowsAppshotProcess; app_id: string; app_label: string; window_title: string;
    window_handle: string; bounds: { x: number; y: number; width: number; height: number };
  };
  png: { name: string; size: number; width: number; height: number; sha256: string; identity: WindowsAppshotFileIdentity };
  uia: {
    name: string; size: number; sha256: string; identity: WindowsAppshotFileIdentity;
    coverage: "reported_uia_subtree" | "unavailable"; node_count: number; depth: number;
    truncated: boolean; truncation_reasons: string[];
  };
  broker: { instance_id: string; session_id: string; recipient: WindowsAppshotProcess };
}

function fail(): never { throw new AppshotValidationError("invalid_windows_contract"); }
function windowsRule(value: unknown, rule: Rule): unknown {
  if (typeof rule === "string") return model(value, models[rule]);
  if (rule[0] === "sid") {
    if (typeof value !== "string") fail();
    const parts = value.split("-");
    if (parts.length < 4 || parts.length > 18 || parts[0] !== "S" || parts[1] !== "1") fail();
    for (const [i, part] of parts.slice(2).entries()) {
      if (!/^(0|[1-9][0-9]{0,14})$/.test(part) || /[\r\n]/.test(part)
        || BigInt(part) > (i === 0 ? 281474976710655n : 4294967295n)) fail();
    }
    return value;
  }
  if (rule[0] === "windowsPath") {
    if (typeof value !== "string" || Buffer.byteLength(value) > 4096 || !/^[A-Z]:\//.test(value)) fail();
    for (const part of value.slice(3).split("/")) {
      if (!part || part === "." || part === ".." || /[. ]$/.test(part)
        || /[\x00-\x1f<>:"\\|?*]/.test(part)
        || /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$/i.test(part)) fail();
    }
    return value;
  }
  return appshotRule(value, rule);
}
function model(value: unknown, fields: Fields): Record<string, unknown> {
  if (!fields) fail();
  const object = appshotObject(value, Object.keys(fields));
  return Object.fromEntries(Object.entries(fields).map(([key, rule]) => [key, windowsRule(object[key], rule)]));
}
export function parseWindowsAppshotManifest(raw: string | Uint8Array): WindowsAppshotManifest {
  const m = model(decodeAppshotJSON(raw), models.WindowsManifest) as unknown as WindowsAppshotManifest;
  if (m.source.window_handle === "0" || m.source.process.process_start === "0"
    || m.broker.recipient.process_start === "0" || m.png.width * m.png.height > 32000000
    || m.png.name !== `appshot-${m.token}.png` || m.uia.name !== `appshot-${m.token}.uia.json`
    || [m.png.identity.owner_sid, m.uia.identity.owner_sid, m.source.process.user_sid]
      .some(sid => sid !== m.broker.recipient.user_sid)) fail();
  if (m.uia.coverage === "unavailable" && (m.uia.node_count !== 0 || m.uia.depth !== 0
    || !m.uia.truncated || !m.uia.truncation_reasons.length)) fail();
  return m;
}
export function parseWindowsAppshotMessage(raw: string | Uint8Array): WindowsAppshotMessage {
  const v = decodeAppshotJSON(raw);
  if (!v || typeof v !== "object" || !("type" in v) || typeof v.type !== "string") fail();
  const message = model(v, messages[v.type]);
  if (v.type === "hello" && message.process_start === "0") fail();
  return message as unknown as WindowsAppshotMessage;
}

export function parseWindowsAppshotProcess(value: unknown): WindowsAppshotProcess {
  const process = model(value, models.WindowsProcess) as unknown as WindowsAppshotProcess;
  if (process.process_start === "0") fail();
  return process;
}
export function parseWindowsAppshotBrokerMessage(raw: string | Uint8Array): WindowsAppshotBrokerMessage {
  const message = parseWindowsAppshotMessage(raw);
  switch (message.type) {
    case "hello_ack": case "attach_offer": case "attach_commit": case "attach_revoke":
    case "release_ack": case "command_result": case "client_state_ack": case "status": return message;
    default: throw new AppshotValidationError("wrong_direction");
  }
}
export function encodeWindowsAppshotMessage(message: WindowsAppshotMessage): Buffer {
  const raw = JSON.stringify(message);
  parseWindowsAppshotMessage(raw);
  if (Buffer.byteLength(raw) + 1 > 65536) throw new AppshotValidationError("frame_size");
  return Buffer.from(raw + "\n");
}
export class WindowsAppshotFrameDecoder {
  private bytes: number[] = [];
  feed(chunk: Uint8Array): WindowsAppshotMessage[] {
    const result: WindowsAppshotMessage[] = [];
    for (const byte of chunk) {
      if (this.bytes.length + 1 > 65536) {
        this.bytes = []; throw new AppshotValidationError("frame_size");
      }
      if (byte === 10) {
        const raw = Buffer.from(this.bytes); this.bytes = [];
        result.push(parseWindowsAppshotMessage(raw));
      } else this.bytes.push(byte);
    }
    return result;
  }
  finish() {
    if (this.bytes.length) { this.bytes = []; throw new AppshotValidationError("incomplete_frame"); }
  }
}
