import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult, ReceiptDevice, ReceiptPair } from "./api";
import { ReceiptDevices } from "./devices";
import { isAccessLoss } from "./model";

type DeviceApi = Pick<BudgetApi, "receiptDevices" | "pairReceiptDevice" | "revokeReceiptDevice">;

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> {
  return Promise.resolve({ ok: false, status, error });
}

function api(devices: ReceiptDevice[] = []) {
  return {
    receiptDevices: vi.fn<BudgetApi["receiptDevices"]>(() => ok(devices)),
    pairReceiptDevice: vi.fn<BudgetApi["pairReceiptDevice"]>(),
    revokeReceiptDevice: vi.fn<BudgetApi["revokeReceiptDevice"]>(),
  } satisfies DeviceApi;
}

const device: ReceiptDevice = {
  token_id: "11111111-1111-4111-8111-111111111111",
  device_name: "Vesta", created_at: 1, last_used_at: null, expires_at: 2,
};

function pairing(): ReceiptPair {
  const code = Array.from({ length: 8 }, (_, index) => String(index)).join("");
  return { code, expires_at: Math.floor(Date.now() / 1000) + 300, qr_payload: `hestia-pair:${code}` };
}

let host: HTMLElement;
let root: Root;
const onAccessLoss = vi.fn(isAccessLoss);

beforeEach(() => {
  onAccessLoss.mockClear();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => { root.unmount(); await Promise.resolve(); });
  host.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function render(client: DeviceApi, locale = "en"): Promise<void> {
  await act(async () => {
    root.render(<ReceiptDevices api={client} locale={locale} revision={0} onAccessLoss={onAccessLoss} />);
    await Promise.resolve();
  });
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
    const pair = pairing();
    client.pairReceiptDevice.mockImplementation(() => ok(pair));
    client.revokeReceiptDevice.mockImplementation(() => ok({ ok: true }));
    await render(client, "pl");
    expect(host.textContent).toContain("Urządzenia");
    expect(host.textContent).toContain("Vesta");
    await act(async () => { button("Dodaj urządzenie").click(); await Promise.resolve(); });
    expect(host.querySelector(".budget-pair-code")?.textContent).toBe(pair.code);
    expect(host.querySelector("svg")?.getAttribute("aria-label")).toBe("Kod QR parowania");
    await act(async () => { button("Odwołaj").click(); await Promise.resolve(); });
    expect(client.revokeReceiptDevice).toHaveBeenCalledWith(device.token_id);
    expect(host.textContent).not.toContain("Vesta");
    client.receiptDevices.mockImplementation(() => ok([{ ...device, device_name: "Recently paired" }]));
    await act(async () => { button("Zamknij").click(); await Promise.resolve(); });
    expect(host.querySelector(".budget-pair-code")).toBeNull();
    expect(host.textContent).toContain("Recently paired");
  });

  it("says the QR is unavailable when the payload does not fit", async () => {
    const client = api();
    client.pairReceiptDevice.mockImplementation(() => ok({ ...pairing(), qr_payload: "x".repeat(40) }));
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
    expect(host.textContent).toContain("The pairing code could not be created.");
    expect(host.textContent).toContain("Vesta");
  });

  it("shows the local expiry in the UI locale and ticks the countdown", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 10, 23, 12, 0, 0));
    const client = api();
    const pair = { ...pairing(), expires_at: Date.now() / 1000 + 65 };
    client.pairReceiptDevice.mockImplementation(() => ok(pair));
    await render(client, "pl");
    await act(async () => { button("Dodaj urządzenie").click(); await Promise.resolve(); });
    const expiry = new Date(pair.expires_at * 1000);
    const localTime = new Intl.DateTimeFormat("pl", { hour: "2-digit", minute: "2-digit" }).format(expiry);
    expect(host.querySelector(".budget-pair")?.textContent).toContain(`Ważny do: ${localTime}`);
    expect(host.querySelector(".budget-pair")?.textContent).toContain("Pozostało: 01:05");
    expect(host.querySelector(".budget-pair")?.textContent).not.toContain(expiry.toISOString());
    expect(host.querySelector('[role="timer"]')?.textContent).toBe("01:05");
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(host.querySelector('[role="timer"]')?.textContent).toBe("01:04");
  });

  it("includes the date when the pairing code expires on another day", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 10, 23, 23, 59, 30));
    const client = api();
    const pair = { ...pairing(), expires_at: Date.now() / 1000 + 65 };
    client.pairReceiptDevice.mockImplementation(() => ok(pair));
    await render(client);
    await act(async () => { button("Add device").click(); await Promise.resolve(); });
    const localDateTime = new Intl.DateTimeFormat("en", {
      day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    }).format(new Date(pair.expires_at * 1000));
    expect(host.querySelector(".budget-pair")?.textContent).toContain(`Expires: ${localDateTime}`);
  });

  it("clears the pairing data as soon as it expires", async () => {
    vi.useFakeTimers();
    const client = api();
    client.pairReceiptDevice.mockImplementation(() => ok(pairing()));
    await render(client);
    await act(async () => { button("Add device").click(); await Promise.resolve(); });
    expect(host.querySelector("svg")).not.toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(300_000); });
    expect(host.querySelector(".budget-pair")).toBeNull();
  });

  it("keeps the device on a failed revoke and reports loss of access to the budget", async () => {
    const client = api([device]);
    client.revokeReceiptDevice.mockImplementationOnce(() => fail(503, "budget_unavailable"));
    client.revokeReceiptDevice.mockImplementationOnce(() => fail(403, "budget_http_403"));
    await render(client);
    await act(async () => { button("Revoke").click(); await Promise.resolve(); });
    expect(host.textContent).toContain("The device could not be revoked.");
    expect(host.textContent).toContain("Vesta");
    await act(async () => { button("Revoke").click(); await Promise.resolve(); });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 403, error: "budget_http_403" });
  });

  it("hides the panel when a mutation reports that receipts have been disabled", async () => {
    const client = api([device]);
    client.pairReceiptDevice.mockImplementation(() => fail(404, "budget_receipts_disabled"));
    await render(client);
    await act(async () => { button("Add device").click(); await Promise.resolve(); });
    expect(host.textContent).toBe("");
  });
});
