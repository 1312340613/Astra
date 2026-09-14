import {
  constants,
  openSync,
  fstatSync,
  lstatSync,
  readSync,
  closeSync,
  realpathSync,
} from "node:fs";
import { dirname, basename } from "node:path";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import {
  parseAppshotManifest,
  type AppshotAttachOffer,
  type AppshotBrokerBinding,
} from "./appshot-protocol.js";
import type { InputSubmission } from "./types.js";
import type { WindowsAppshotBinding } from "./appshot-protocol-windows.js";

export type AppshotInputOffer = {
  requestId: string;
  manifestPath: string;
  appLabel: string;
  windowTitle: string;
  screenshotOnly?: boolean;
  binding: AppshotBrokerBinding | WindowsAppshotBinding;
};
export type AppshotInputAttachment = AppshotInputOffer & {
  label: string;
  number: number;
  start: number;
};
export type AppshotInputDraft = {
  text: string;
  attachments: AppshotInputAttachment[];
  nextNumber: number;
};
export type AppshotInputState = {
  draft: AppshotInputDraft;
  pending?: {
    submissionId: string;
    draft: AppshotInputDraft;
    status: "pending" | "unknown";
    revokedRequestIds: string[];
  };
};
export const emptyDraft = (nextNumber = 1): AppshotInputDraft => ({
  text: "",
  attachments: [],
  nextNumber,
});
export const appshotCount = (s: AppshotInputState) =>
  s.draft.attachments.length + (s.pending?.draft.attachments.length ?? 0);

function display(value: string, width: number) {
  const clean = stripAnsi(value)
    .replace(
      /[\u0000-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g,
      " ",
    )
    .replace(/\s+/g, " ")
    .trim();
  if (stringWidth(clean) <= width) return clean;
  let out = "";
  for (const c of clean) {
    if (stringWidth(out + c) > width - 1) break;
    out += c;
  }
  return out + "…";
}
export function appendAppshot(
  draft: AppshotInputDraft,
  offer: AppshotInputOffer,
  cursor = draft.text.length,
): AppshotInputDraft {
  if (draft.attachments.length >= 4)
    throw new Error("attachment_limit_reached");
  if (draft.attachments.some((a) => a.requestId === offer.requestId))
    throw new Error("attachment_rejected");
  const number = draft.nextNumber;
  const label = `[Appshot #${number} · ${display(offer.appLabel, 24)} · ${display(offer.windowTitle, 48)}${offer.screenshotOnly ? " · 仅截图" : ""}]`;
  const start = Math.max(0, Math.min(cursor, draft.text.length));
  // Insertion inside a token replaces that token at its original boundary.
  const touched = draft.attachments.find(
    (a) => start > a.start && start < a.start + a.label.length,
  );
  const base = touched
    ? reconcileAppshotInput(
        draft,
        draft.text.slice(0, touched.start) +
          draft.text.slice(touched.start + touched.label.length),
      )
    : draft;
  const at = touched?.start ?? start;
  return {
    text: base.text.slice(0, at) + label + base.text.slice(at),
    nextNumber: number + 1,
    attachments: [
      ...base.attachments.map((a) => ({
        ...a,
        start: a.start >= at ? a.start + label.length : a.start,
      })),
      { ...offer, label, number, start: at },
    ],
  };
}

/** Reconcile positional identity using a contiguous edit, never parse identity from editable text. */
export function reconcileAppshotInput(
  draft: AppshotInputDraft,
  text: string,
): AppshotInputDraft {
  if (text === draft.text) return draft;
  let lo = 0;
  while (
    lo < Math.min(text.length, draft.text.length) &&
    text[lo] === draft.text[lo]
  )
    lo++;
  let oldEnd = draft.text.length,
    newEnd = text.length;
  while (
    oldEnd > lo &&
    newEnd > lo &&
    draft.text[oldEnd - 1] === text[newEnd - 1]
  ) {
    oldEnd--;
    newEnd--;
  }
  // Whole-label deletion can share a prefix with its neighbour. Prefer its exact
  // known positional boundary over an ambiguous longest-prefix text diff.
  const deleted = draft.text.length - text.length;
  if (deleted > 0) {
    const boundary = draft.attachments.find(
      (a) =>
        deleted >= a.label.length &&
        draft.text.slice(0, a.start) + draft.text.slice(a.start + deleted) ===
          text,
    );
    if (boundary) {
      lo = boundary.start;
      oldEnd = lo + deleted;
      newEnd = lo;
    }
  }
  const delta = newEnd - oldEnd;
  const removals: Array<[number, number]> = [];
  let attachments: AppshotInputAttachment[] = [];
  for (const a of draft.attachments) {
    const end = a.start + a.label.length;
    const touched =
      oldEnd > lo ? lo < end && oldEnd > a.start : lo > a.start && lo < end;
    if (!touched) {
      attachments.push({
        ...a,
        start: a.start >= oldEnd ? a.start + delta : a.start,
      });
      continue;
    }
    if (a.start < lo) removals.push([a.start, lo]);
    if (end > oldEnd) removals.push([newEnd, end + delta]);
    if (lo >= a.start && oldEnd <= end) removals.push([lo, newEnd]);
  }
  removals.sort((a, b) => b[0] - a[0]);
  for (const [start, end] of removals) {
    text = text.slice(0, start) + text.slice(end);
    attachments = attachments.map((a) => ({
      ...a,
      start: a.start >= end ? a.start - (end - start) : a.start,
    }));
  }
  // Duplicate presentation text is ordinary only outside every owned range.
  return mapAppshotOrdinaryText({ ...draft, text, attachments }, (ordinary) => {
    let out = "", cursor = 0;
    while (cursor < ordinary.length) {
      const match = draft.attachments
        .map(a => ({ a, at: ordinary.indexOf(a.label, cursor) }))
        .filter(m => m.at >= 0)
        .sort((a, b) => a.at - b.at || b.a.label.length - a.a.label.length)[0];
      if (!match) break;
      out += ordinary.slice(cursor, match.at) + `(Appshot #${match.a.number})`;
      cursor = match.at + match.a.label.length;
    }
    return out + ordinary.slice(cursor);
  });
}

/** Transform only user-authored gaps; preserve opaque labels and shift their typed positions. */
export function mapAppshotOrdinaryText(
  draft: AppshotInputDraft,
  transform: (text: string) => string,
): AppshotInputDraft {
  let text = "", end = 0;
  const starts = new Map<string, number>();
  for (const a of [...draft.attachments].sort((a, b) => a.start - b.start)) {
    text += transform(draft.text.slice(end, a.start));
    starts.set(a.requestId, text.length);
    text += a.label;
    end = a.start + a.label.length;
  }
  text += transform(draft.text.slice(end));
  return { ...draft, text, attachments: draft.attachments.map(a => ({ ...a, start: starts.get(a.requestId)! })) };
}

/** Project immutable owned ranges, independent of token contents or attachment insertion order. */
export function projectAppshotText(
  draft: AppshotInputDraft,
  label: (attachment: AppshotInputAttachment) => string,
): string {
  let text = draft.text;
  for (const a of [...draft.attachments].sort((a, b) => b.start - a.start))
    text = text.slice(0, a.start) + label(a) + text.slice(a.start + a.label.length);
  return text;
}

export function appshotSubmission(
  draft: AppshotInputDraft,
  submissionId: string,
): InputSubmission {
  const text = projectAppshotText(draft, a => `[Appshot #${a.number}]`);
  const first = draft.attachments[0];
  if (
    draft.attachments.some(
      (a) =>
        a.binding.instance_id !== first.binding.instance_id ||
        a.binding.session_id !== first.binding.session_id,
    )
  )
    throw new Error("recipient_disconnected");
  return {
    text,
    appshots: draft.attachments.map((a) => ({
      label: `[Appshot #${a.number}]`,
      manifest_path: a.manifestPath,
    })),
    submissionId,
    appshotSessionId: first?.binding.session_id,
    appshotBrokerId: first?.binding.instance_id,
  };
}
export function freezeAppshotSubmission(
  state: AppshotInputState,
  submissionId: string,
): AppshotInputState {
  if (state.pending) throw new Error("submission_pending");
  return {
    draft: emptyDraft(state.draft.nextNumber),
    pending: {
      submissionId,
      draft: state.draft,
      status: "pending",
      revokedRequestIds: [],
    },
  };
}
export function settleAppshotSubmission(
  state: AppshotInputState,
  id: string,
  result: "accepted" | "rejected" | "unknown",
): AppshotInputState {
  if (state.pending?.submissionId !== id) return state;
  if (result === "unknown")
    return { ...state, pending: { ...state.pending, status: "unknown" } };
  if (result === "accepted") return { draft: state.draft };
  const prior = removeAttachments(
    state.pending.draft,
    new Set(state.pending.revokedRequestIds),
  );
  const separator = prior.text && state.draft.text ? "\n" : "";
  const shift = prior.text.length + separator.length;
  return {
    draft: {
      text: prior.text + separator + state.draft.text,
      nextNumber: state.draft.nextNumber,
      attachments: [
        ...prior.attachments,
        ...state.draft.attachments.map((a) => ({
          ...a,
          start: a.start + shift,
        })),
      ],
    },
  };
}
function removeAttachments(
  draft: AppshotInputDraft,
  ids?: Set<string>,
): AppshotInputDraft {
  let next = draft;
  for (const a of [...draft.attachments].sort((a, b) => b.start - a.start))
    if (!ids || ids.has(a.requestId))
      next = reconcileAppshotInput(
        next,
        next.text.slice(0, a.start) + next.text.slice(a.start + a.label.length),
      );
  return next;
}
export function revokeAppshots(
  state: AppshotInputState,
  ids?: Set<string>,
): AppshotInputState {
  const affected =
    !ids || state.pending?.draft.attachments.some((a) => ids.has(a.requestId));
  return {
    draft: removeAttachments(state.draft, ids),
    pending:
      state.pending && affected
        ? {
            ...state.pending,
            status: "unknown",
            revokedRequestIds: [
              ...new Set([
                ...state.pending.revokedRequestIds,
                ...state.pending.draft.attachments
                  .filter((a) => !ids || ids.has(a.requestId))
                  .map((a) => a.requestId),
              ]),
            ],
          }
        : state.pending,
  };
}

/** Production reader: fixed canonical private runtime, owner-only held parent and nonblocking bounded nofollow file. */
export function readAppshotManifest(path: string): string {
  if (typeof process.getuid !== "function") throw new Error("artifact_unsafe");
  const uid = process.getuid();
  return readAppshotManifestInRuntime(
    path,
    `/private/tmp/astra-appshot-${uid}`,
    uid,
  );
}
/** Private fixture seam; production callers always use the fixed-runtime wrapper above. */
export function readAppshotManifestInRuntime(
  path: string,
  runtime: string,
  uid: number,
): string {
  if (
    dirname(path) !== runtime ||
    !/^appshot-[a-f0-9]{32}\.manifest\.json$/.test(basename(path)) ||
    realpathSync(runtime) !== runtime
  )
    throw new Error("artifact_unsafe");
  const parent = openSync(
    runtime,
    constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
  );
  let fd: number | undefined;
  try {
    const dir = fstatSync(parent, { bigint: true });
    if (
      !dir.isDirectory() ||
      dir.uid !== BigInt(uid) ||
      (dir.mode & 4095n) !== 448n
    )
      throw new Error("artifact_unsafe");
    fd = openSync(
      path,
      constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
    );
    const stat = fstatSync(fd, { bigint: true });
    if (
      !stat.isFile() ||
      stat.uid !== BigInt(uid) ||
      (stat.mode & 4095n) !== 384n ||
      stat.nlink !== 1n ||
      stat.size > 65536n
    )
      throw new Error("artifact_unsafe");
    const bytes = Buffer.alloc(65537);
    let n = 0,
      r = 0;
    while (
      n < bytes.length &&
      (r = readSync(fd, bytes, n, bytes.length - n, null)) > 0
    )
      n += r;
    const after = fstatSync(fd, { bigint: true }),
      named = lstatSync(path, { bigint: true }),
      namedDir = lstatSync(runtime, { bigint: true });
    if (
      n > 65536 ||
      BigInt(n) !== stat.size ||
      after.size !== stat.size ||
      after.mtimeNs !== stat.mtimeNs ||
      after.ctimeNs !== stat.ctimeNs ||
      named.mode !== stat.mode ||
      named.uid !== stat.uid ||
      named.nlink !== stat.nlink ||
      named.dev !== stat.dev ||
      named.ino !== stat.ino ||
      namedDir.dev !== dir.dev ||
      namedDir.ino !== dir.ino ||
      namedDir.mode !== dir.mode ||
      namedDir.uid !== dir.uid
    )
      throw new Error("artifact_unsafe");
    return new TextDecoder("utf-8", { fatal: true }).decode(
      bytes.subarray(0, n),
    );
  } finally {
    if (fd !== undefined) closeSync(fd);
    closeSync(parent);
  }
}
export type AppshotManifestReader = (path: string) => string;
export function validateAppshotOffer(
  offer: AppshotAttachOffer,
  binding: AppshotBrokerBinding,
  reader: AppshotManifestReader = readAppshotManifest,
): AppshotInputOffer {
  const m = parseAppshotManifest(reader(offer.manifest_path));
  if (
    offer.broker_id !== binding.instance_id ||
    offer.session_id !== binding.session_id ||
    Object.keys(binding).some(
      (k) =>
        m.broker[k as keyof AppshotBrokerBinding] !==
        binding[k as keyof AppshotBrokerBinding],
    ) ||
    basename(offer.manifest_path) !== `appshot-${m.token}.manifest.json`
  )
    throw new Error("artifact_unsafe");
  return {
    requestId: offer.request_id,
    manifestPath: offer.manifest_path,
    appLabel: m.source.app_label,
    windowTitle: m.source.window_title,
    ...(m.ax.coverage === "unavailable" ? { screenshotOnly: true } : {}),
    binding: { ...binding },
  };
}
