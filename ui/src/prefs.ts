// Client-side user preferences, persisted in localStorage. For logged-in users the server's
// per-user settings row is authoritative; this is the local-first cache/fallback layer.
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

/** Returns whether the write actually persisted (false when storage is unavailable). */
function write(key: string, value: string): boolean {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch {
    return false; // storage unavailable (Safari private mode etc.) — the caller decides what to do
  }
}

/** The user's explicit locale override (chosen in the menu), or null to use browser detection. */
export function localeOverride(): string | null {
  return read(LOCALE_KEY);
}

export function setLocaleOverride(code: string): boolean {
  return write(LOCALE_KEY, code);
}

/** The user's temperature scale; Celsius by default (the sensors' native scale). */
export function tempScale(): TempScale {
  const v = read(SCALE_KEY);
  return v === "F" || v === "K" ? v : "C";
}

export function setTempScale(scale: TempScale): boolean {
  return write(SCALE_KEY, scale);
}
