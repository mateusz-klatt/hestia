// Client-side user preferences, persisted in localStorage. Server-side per-user persistence
// (synced across devices) lands with the settings work; this is the local-first layer.
// All access is throw-safe (Safari private mode / disabled storage just falls back to defaults).

const LOCALE_KEY = "hestia.locale";
const SCALE_KEY = "hestia.tempScale";

export type TempScale = "C" | "F" | "K";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage unavailable — the choice just won't persist */
  }
}

/** The user's explicit locale override (chosen in the menu), or null to use browser detection. */
export function localeOverride(): string | null {
  return read(LOCALE_KEY);
}

export function setLocaleOverride(code: string): void {
  write(LOCALE_KEY, code);
}

/** The user's temperature scale; Celsius by default (the sensors' native scale). */
export function tempScale(): TempScale {
  const v = read(SCALE_KEY);
  return v === "F" || v === "K" ? v : "C";
}

export function setTempScale(scale: TempScale): void {
  write(SCALE_KEY, scale);
}
