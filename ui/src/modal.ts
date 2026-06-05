import type { MutationResult } from "./api/client";
import { t } from "./i18n";

/** Minimum password length — mirrors the backend (hestia/web.py `_MIN_PASSWORD`). The server is the
 *  authority; this lets the UI reject the common case with a localized message instead of a raw 400. */
export const MIN_PASSWORD = 8;

export interface ModalField {
  name: string;
  label: string;
  autocomplete?: string;
}

export interface FormModalOptions {
  title: string;
  fields: ModalField[]; // all rendered as password inputs
  submitLabel: string;
  successText: string;
  onSubmit: (values: Record<string, string>) => Promise<MutationResult>;
  onClosed?: () => void;
}

/**
 * A centred modal with a small password form. Closes shortly after success (a brief ✓), shows the
 * error otherwise (the caller's localized message, else the server's, else a generic fallback).
 * XSS-safe (textContent + DOM, no innerHTML). Returns a `close()` handle (used by tests). Appends to
 * document.body; closes on the Cancel button, an overlay-backdrop click, or Escape.
 */
export function openFormModal(opts: FormModalOptions): () => void {
  const previousFocus = document.activeElement; // restored on close so focus returns to the trigger

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";

  const card = document.createElement("div");
  card.className = "modal-card";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-modal", "true");
  card.setAttribute("aria-label", opts.title);

  const heading = document.createElement("h3");
  heading.textContent = opts.title;

  const form = document.createElement("form");
  form.className = "modal-form";
  const inputs = new Map<string, HTMLInputElement>();
  for (const field of opts.fields) {
    const row = document.createElement("label");
    row.className = "modal-row";
    row.append(`${field.label} `);
    const input = document.createElement("input");
    input.type = "password";
    input.name = field.name;
    input.setAttribute("autocomplete", field.autocomplete ?? "off");
    input.setAttribute("aria-label", field.label);
    row.appendChild(input);
    form.appendChild(row);
    inputs.set(field.name, input);
  }

  const status = document.createElement("span");
  status.className = "status";
  status.setAttribute("aria-live", "polite");

  const actions = document.createElement("div");
  actions.className = "modal-actions";
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = opts.submitLabel;
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "modal-cancel";
  cancel.textContent = t("modal.cancel");
  actions.append(cancel, submit);

  let closed = false;
  const onKey = (event: KeyboardEvent): void => {
    if (event.key === "Escape") close();
  };
  function close(): void {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    overlay.remove();
    if (previousFocus instanceof HTMLElement) previousFocus.focus(); // return focus to the trigger
    opts.onClosed?.();
  }

  cancel.addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close(); // backdrop click (not a click inside the card)
  });
  document.addEventListener("keydown", onKey);

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void (async () => {
      submit.disabled = true;
      status.textContent = "…";
      status.className = "status";
      const values: Record<string, string> = {};
      for (const [name, input] of inputs) values[name] = input.value;
      const result = await opts.onSubmit(values);
      if (result.ok) {
        status.textContent = opts.successText;
        status.className = "status ok";
        window.setTimeout(close, 600);
      } else {
        status.textContent = result.error ?? t("modal.error");
        status.className = "status err";
        submit.disabled = false;
      }
    })();
  });

  form.append(actions, status);
  card.append(heading, form);
  overlay.appendChild(card);
  document.body.appendChild(overlay);
  const first = opts.fields[0];
  if (first !== undefined) inputs.get(first.name)?.focus();
  return close;
}
