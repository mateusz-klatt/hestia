import { afterEach, describe, expect, it, vi } from "vitest";

import { openFormModal } from "./modal";
import { nth, q } from "./test-dom";

const flush = (): Promise<void> =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

afterEach(() => {
  document.body.replaceChildren();
});

function overlay(): HTMLElement | null {
  return document.querySelector(".modal-overlay");
}

function submitForm(): void {
  q(document, "form").dispatchEvent(new Event("submit"));
}

describe("openFormModal", () => {
  const baseOpts = {
    title: "Change password",
    fields: [
      { name: "current", label: "Current" },
      { name: "next", label: "New" },
    ],
    submitLabel: "Save",
    successText: "✓ done",
  };

  it("renders the title, a password input per field, and submit/cancel", () => {
    openFormModal({ ...baseOpts, onSubmit: () => Promise.resolve({ ok: true, error: null }) });
    expect(document.querySelector(".modal-card h3")?.textContent).toBe("Change password");
    const inputs = document.querySelectorAll<HTMLInputElement>(".modal-form input");
    expect(inputs).toHaveLength(2);
    expect([...inputs].every((i) => i.type === "password")).toBe(true);
    expect(document.querySelector("button[type=submit]")?.textContent).toBe("Save");
    expect(document.querySelector(".modal-cancel")?.textContent).toBe("Cancel");
  });

  it("submits the field values and shows success, then auto-closes", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true, error: null });
    const closed = vi.fn();
    openFormModal({ ...baseOpts, onSubmit, onClosed: closed });
    nth<HTMLInputElement>(document, ".modal-form input", 0).value = "old";
    nth<HTMLInputElement>(document, ".modal-form input", 1).value = "newsecret";
    submitForm();
    await flush();
    expect(onSubmit).toHaveBeenCalledWith({ current: "old", next: "newsecret" });
    expect(document.querySelector(".status")?.textContent).toBe("✓ done");
    await new Promise((resolve) => setTimeout(resolve, 650)); // the 600ms success auto-close
    expect(overlay()).toBeNull();
    expect(closed).toHaveBeenCalledTimes(1);
  });

  it("shows the error and keeps the modal open on failure", async () => {
    openFormModal({ ...baseOpts, onSubmit: () => Promise.resolve({ ok: false, error: "nope" }) });
    submitForm();
    await flush();
    expect(document.querySelector(".status")?.textContent).toBe("nope");
    expect(q<HTMLButtonElement>(document, "button[type=submit]").disabled).toBe(false);
    expect(overlay()).not.toBeNull();
  });

  it("falls back to a generic error when the result carries none", async () => {
    openFormModal({ ...baseOpts, onSubmit: () => Promise.resolve({ ok: false, error: null }) });
    submitForm();
    await flush();
    expect(document.querySelector(".status")?.textContent).toBe("Something went wrong");
  });

  it("closes on Cancel, Escape, a backdrop click, and the returned handle; onClosed fires once", () => {
    const onClosed = vi.fn();
    const close = openFormModal({ ...baseOpts, onSubmit: () => Promise.resolve({ ok: true, error: null }), onClosed });
    q<HTMLButtonElement>(document, ".modal-cancel").click();
    expect(overlay()).toBeNull();
    expect(onClosed).toHaveBeenCalledTimes(1);
    close(); // idempotent — no double onClosed
    expect(onClosed).toHaveBeenCalledTimes(1);

    openFormModal({ ...baseOpts, onSubmit: () => Promise.resolve({ ok: true, error: null }) });
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(overlay()).toBeNull();

    openFormModal({ ...baseOpts, onSubmit: () => Promise.resolve({ ok: true, error: null }) });
    q(document, ".modal-overlay").dispatchEvent(new MouseEvent("click", { bubbles: true })); // target === overlay
    expect(overlay()).toBeNull();
  });
});
