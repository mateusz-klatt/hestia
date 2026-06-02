import type { DeviceInfo, NamePayload, NameResult } from "./api/types";

/** Sends one registry mutation; returns ok + body (never rejects). */
export type PostName = (payload: NamePayload) => Promise<NameResult>;

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
  const run = async (): Promise<void> => {
    const r = await postName(payload());
    setStatus(tr, controlClass, r.ok ? successText : r.body, !r.ok);
    // A successful save makes the server publish discovery_changed → the SSE
    // stream re-syncs this row; no manual refresh needed.
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
  const inferred = info.type;
  wire(tr, tr.querySelector(".confirm"), "confirm", "confirmed", () => ({ node, type: inferred }), postName);
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
