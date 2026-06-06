import type { DeviceInfo, NamePayload, NameResult } from "./api/types";

/** Sends one registry mutation; returns ok + body (never rejects). */
export type PostName = (payload: NamePayload) => Promise<NameResult>;

type DeviceType = NonNullable<NamePayload["type"]>;

// The DeviceType set, as an EXHAUSTIVE map: `Record<DeviceType, true>` makes a missing or stray key a
// compile error, so this stays in sync with the contract's NameRequest.type literal automatically.
const DEVICE_TYPES: Record<DeviceType, true> = {
  light: true, blind: true, thermostat: true, door: true, motion: true,
  smoke: true, water: true, plug: true, unknown: true,
};

/** A device's `type` is loosely `string` in the contract (the registry is stored losslessly); narrow it
 *  to a real DeviceType so we never wire a "confirm" that /api/name would reject. */
function isDeviceType(t: string): t is DeviceType {
  return Object.prototype.hasOwnProperty.call(DEVICE_TYPES, t);
}

/** Write the status text next to the clicked control (its cell's `.status` span). */
function setStatus(tr: HTMLElement, controlClass: string, text: string, isErr: boolean): void {
  const control = tr.querySelector(`.${controlClass}`);
  const status = control?.parentElement?.querySelector(".status") ?? null;
  if (status === null) return;
  status.textContent = text;
  status.className = isErr ? "status err" : "status";
}

/** Wire one Save/confirm button: POST on click, then show "saved"/"confirmed" or the error body. */
function wire(
  tr: HTMLElement,
  button: HTMLButtonElement | null,
  controlClass: string,
  successText: string,
  payload: () => NamePayload,
  postName: PostName,
): void {
  if (button === null) return;
  let busy = false;
  const run = async (): Promise<void> => {
    if (busy) return; // one POST per button in flight — no duplicate mutations / status race
    busy = true;
    button.disabled = true;
    try {
      const r = await postName(payload());
      setStatus(tr, controlClass, r.ok ? successText : r.body, !r.ok);
      // A successful save makes the server publish discovery_changed → the SSE
      // stream re-syncs this row; no manual refresh needed.
    } finally {
      busy = false;
      button.disabled = false;
    }
  };
  button.addEventListener("click", () => {
    void run();
  });
}

/**
 * Wire a node row's registry mutations: confirm the inferred type, and save the
 * name / room labels. (The server validates; its error body is surfaced verbatim.)
 */
export function bindRow(tr: HTMLElement, node: number, info: DeviceInfo, postName: PostName): void {
  // The "confirm" action sends the inferred type back to /api/name (which accepts only a DeviceType).
  // Wire it only when the type is a real DeviceType — so a corrupt/hand-edited registry value can't be
  // turned into a request the server would reject.
  if (isDeviceType(info.type)) {
    const inferred = info.type;
    wire(tr, tr.querySelector(".confirm"), "confirm", "confirmed", () => ({ node, type: inferred }), postName);
  }
  const nameInput = tr.querySelector<HTMLInputElement>(".name");
  if (nameInput !== null) {
    wire(tr, tr.querySelector(".save-name"), "save-name", "saved", () => ({ node, name: nameInput.value }), postName);
  }
  const roomInput = tr.querySelector<HTMLInputElement>(".room");
  if (roomInput !== null) {
    wire(tr, tr.querySelector(".save-room"), "save-room", "saved", () => ({ node, room: roomInput.value }), postName);
  }
}

/** Wire a multi-gang sub-row's per-endpoint label save (sent as a JSON number `ep`). */
export function bindSubRow(tr: HTMLElement, node: number, ep: number, postName: PostName): void {
  const input = tr.querySelector<HTMLInputElement>(".ep-name");
  if (input === null) return;
  wire(tr, tr.querySelector(".save-ep-name"), "save-ep-name", "saved", () => ({ node, ep, name: input.value }), postName);
}
