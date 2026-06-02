import { describe, expect, it } from "vitest";

import type { NamePayload, NameResult } from "./api/types";
import { device } from "./fixtures";
import { bindRow, bindSubRow, type PostName } from "./registry";
import { deviceRow, renderDeviceRows } from "./render/devices";

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
const clickBtn = (root: ParentNode, sel: string): void => {
  root.querySelector<HTMLButtonElement>(sel)?.click();
};
const statusOf = (root: ParentNode, controlSel: string): Element | null =>
  root.querySelector(controlSel)?.parentElement?.querySelector(".status") ?? null;
const okName: PostName = () => Promise.resolve({ ok: true, status: 200, body: "" });

describe("bindRow", () => {
  it("confirms the inferred type and saves the edited name/room with the right payloads", async () => {
    const sent: NamePayload[] = [];
    const post: PostName = (p) => {
      sent.push(p);
      return Promise.resolve({ ok: true, status: 200, body: "" });
    };
    const info = device({ type: "light", confidence: "inferred", name: "lampa", room: "salon" });
    const tr = deviceRow("7", info);
    bindRow(tr, 7, info, post);

    const nameInput = tr.querySelector<HTMLInputElement>("input.name");
    if (nameInput !== null) nameInput.value = "nowa"; // operator edits the field
    const roomInput = tr.querySelector<HTMLInputElement>("input.room");
    if (roomInput !== null) roomInput.value = "kuchnia";

    clickBtn(tr, ".confirm");
    await flush();
    clickBtn(tr, ".save-name");
    await flush();
    clickBtn(tr, ".save-room");
    await flush();

    expect(sent).toEqual([
      { node: 7, type: "light" }, // confirm the inferred type
      { node: 7, name: "nowa" }, // the edited value, not the original
      { node: 7, room: "kuchnia" },
    ]);
  });

  it("shows 'confirmed'/'saved' on success", async () => {
    const info = device({ type: "light", confidence: "inferred" });
    const tr = deviceRow("7", info);
    bindRow(tr, 7, info, okName);
    clickBtn(tr, ".confirm");
    await flush();
    expect(statusOf(tr, ".confirm")?.textContent).toBe("confirmed");
    clickBtn(tr, ".save-name");
    await flush();
    expect(statusOf(tr, ".save-name")?.textContent).toBe("saved");
  });

  it("surfaces the server error body verbatim on failure", async () => {
    const info = device({ type: "light", confidence: "inferred" });
    const tr = deviceRow("8", info);
    bindRow(tr, 8, info, () => Promise.resolve({ ok: false, status: 400, body: "invalid name" }));
    clickBtn(tr, ".save-name");
    await flush();
    const status = statusOf(tr, ".save-name");
    expect(status?.textContent).toBe("invalid name");
    expect(status?.classList.contains("err")).toBe(true);
  });

  it("locks a button while its save is in flight — no duplicate POST", async () => {
    const gate = deferred<NameResult>();
    let calls = 0;
    const post: PostName = () => {
      calls += 1;
      return gate.promise;
    };
    const info = device({ type: "light", confidence: "inferred" });
    const tr = deviceRow("7", info);
    bindRow(tr, 7, info, post);
    const save = tr.querySelector<HTMLButtonElement>(".save-name");
    save?.click();
    expect(calls).toBe(1);
    expect(save?.disabled).toBe(true);
    if (save !== null) save.disabled = false; // re-enable and re-click: the busy flag still drops it
    save?.click();
    expect(calls).toBe(1);
    gate.resolve({ ok: true, status: 200, body: "" });
    await flush();
    expect(save?.disabled).toBe(false);
  });
});

describe("bindSubRow", () => {
  it("saves the per-endpoint label with ep as a JSON number", async () => {
    const sent: NamePayload[] = [];
    const post: PostName = (p) => {
      sent.push(p);
      return Promise.resolve({ ok: true, status: 200, body: "" });
    };
    const tbody = document.createElement("tbody");
    renderDeviceRows(tbody, {
      "2": device({ type: "light", endpoints: { "1": true, "2": false }, endpoint_names: { "1": "lewy" } }),
    });
    const sub = tbody.querySelector<HTMLTableRowElement>('tr[data-node="2"][data-ep="1"]');
    expect(sub).not.toBeNull();
    if (sub !== null) {
      bindSubRow(sub, 2, 1, post);
      const input = sub.querySelector<HTMLInputElement>("input.ep-name");
      if (input !== null) input.value = "prawy";
      clickBtn(sub, ".save-ep-name");
      await flush();
    }
    expect(sent).toEqual([{ node: 2, ep: 1, name: "prawy" }]);
  });
});
