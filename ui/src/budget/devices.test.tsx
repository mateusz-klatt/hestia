import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult, ReceiptDevice, ReceiptPair } from "./api";
import { ReceiptDevices } from "./devices";

type DeviceApi = Pick<BudgetApi, "receiptDevices" | "pairReceiptDevice" | "revokeReceiptDevice">;

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> {
  return Promise.resolve({ ok: false, status, error });
}

function api(tokens: ReceiptDevice[] = []): DeviceApi {
  return {
    receiptDevices: vi.fn<BudgetApi["receiptDevices"]>(() => ok(tokens)),
    pairReceiptDevice: vi.fn<BudgetApi["pairReceiptDevice"]>(),
    revokeReceiptDevice: vi.fn<BudgetApi["revokeReceiptDevice"]>(),
  };
}

const device: ReceiptDevice = {
  token_id: "11111111-1111-4111-8111-111111111111",
  device_name: "Vesta", created_at: 1, last_used_at: null, expires_at: 2,
};

let host: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => { root.unmount(); await Promise.resolve(); });
  host.remove();
  vi.unstubAllGlobals();
});

async function render(client: DeviceApi, locale = "en"): Promise<void> {
  await act(async () => { root.render(<ReceiptDevices api={client} locale={locale} revision={0} />); });
  await act(async () => { await Promise.resolve(); });
}

function button(text: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((item) => item.textContent === text);
  if (found === undefined) throw new Error(`button ${text} missing`);
  return found;
}

describe("receipt devices", () => {
  it("hides the section when the receipts flag is off", async () => {
    const client = api();
    client.receiptDevices.mockImplementation(() => fail(404, "budget_receipts_disabled"));
    await render(client);
    expect(host.textContent).toBe("");
  });

  it("shows a pairing code and a QR, then revokes a device", async () => {
    const client = api([device]);
    const pair: ReceiptPair = { code: "01234567", expires_at: 1_700_000_300, qr_payload: "hestia-pair:01234567" };
    client.pairReceiptDevice.mockImplementation(() => ok(pair));
    client.revokeReceiptDevice.mockImplementation(() => ok({ ok: true }));
    await render(client, "pl");
    expect(host.textContent).toContain("Urządzenia");
    expect(host.textContent).toContain("Vesta");
    await act(async () => { button("Dodaj urządzenie").click(); await Promise.resolve(); });
    expect(host.querySelector(".budget-pair-code")?.textContent).toBe("01234567");
    expect(host.querySelector("svg")).not.toBeNull();
    await act(async () => { button("Odwołaj").click(); await Promise.resolve(); });
    expect(client.revokeReceiptDevice).toHaveBeenCalledWith(device.token_id);
    expect(host.textContent).not.toContain("Vesta");
  });

  it("says the QR is unavailable when the payload does not fit", async () => {
    const client = api();
    client.pairReceiptDevice.mockImplementation(() => ok({
      code: "01234567", expires_at: 1, qr_payload: "x".repeat(40),
    }));
    await render(client);
    await act(async () => { button("Add device").click(); await Promise.resolve(); });
    expect(host.textContent).toContain("A QR code is unavailable");
    expect(host.querySelector("svg")).toBeNull();
  });

  it("shows an error when pairing fails and keeps the list", async () => {
    const client = api([device]);
    client.pairReceiptDevice.mockImplementation(() => fail(503, "budget_unavailable"));
    await render(client);
    await act(async () => { button("Add device").click(); await Promise.resolve(); });
    expect(host.textContent).toContain("Devices could not be loaded");
    expect(host.textContent).toContain("Vesta");
  });
});
