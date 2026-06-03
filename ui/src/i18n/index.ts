import en, { type Messages, type MessageKey } from "./locales/en";

/** The 45 supported locales (BCP-47), mirroring the snapper language set. */
export const LOCALES = [
  "ar", "bn", "bs", "cs", "da", "de", "el", "en", "es", "fa", "fi", "fil", "fr", "ga", "he",
  "hi", "hr", "hu", "hy", "id", "is", "it", "ja", "ko", "lt", "lv", "ms", "my-MM", "nl", "no",
  "pl", "pt", "ro", "ru", "sk", "sq", "sr", "sv", "sw", "th", "tr", "uk", "vi", "zh-Hant", "zh",
] as const;
export type Locale = (typeof LOCALES)[number];

/** Right-to-left scripts: Arabic, Persian, Hebrew. */
export const RTL: ReadonlySet<string> = new Set(["ar", "fa", "he"]);

// One lazily-loaded chunk per non-default locale (Vite code-splits each dynamic import). English
// is bundled (the synchronous fallback), so only the visitor's actual locale is fetched on top.
// Ship another language by adding a line here + the matching ./locales/<code>.ts file.
const loaders: Partial<Record<Locale, () => Promise<{ default: Partial<Messages> }>>> = {
  pl: () => import("./locales/pl"),
};

let active: Partial<Messages> = en;
let activeLocale: Locale = "en";

export function currentLocale(): Locale {
  return activeLocale;
}

function interpolate(tmpl: string, params?: Record<string, string | number>): string {
  if (params === undefined) return tmpl;
  return tmpl.replace(/\{(\w+)\}/g, (_m, k: string) => (k in params ? String(params[k]) : `{${k}}`));
}

/** Translate `key`; a key missing from the active locale falls back to English. */
export function t(key: MessageKey, params?: Record<string, string | number>): string {
  return interpolate(active[key] ?? en[key], params);
}

/**
 * Plural-aware translate: `Intl.PluralRules` picks the CLDR category for `n`
 * (zero/one/two/few/many/other) → looks up `<key>.<category>`, falling back to `<key>.other`
 * then English. `n` is available to the template as `{n}`.
 */
export function tPlural(key: string, n: number, params?: Record<string, string | number>): string {
  const cat = new Intl.PluralRules(activeLocale).select(n);
  const full = `${key}.${cat}`;
  const other = `${key}.other`;
  const a = active as Record<string, string | undefined>;
  const e = en as Record<string, string | undefined>;
  // Active locale first (its category, then its `.other`) before English — otherwise, since English
  // declares every category, a locale missing its selected category would show English not its own `.other`.
  const tmpl = a[full] ?? a[other] ?? e[full] ?? e[other] ?? full;
  return interpolate(tmpl, { n, ...params });
}

/** Pick the best supported locale for the browser's preference list (exact, then primary subtag). */
export function pickLocale(prefs: readonly string[]): Locale {
  for (const pref of prefs) {
    const lower = pref.toLowerCase();
    const exact = LOCALES.find((l) => l.toLowerCase() === lower);
    if (exact !== undefined) return exact;
    const primary = lower.split("-")[0] ?? lower;
    // Chinese: the bare-primary rule below would send every zh-* to "zh", but Traditional Chinese
    // (Hant script, or the TW/HK/MO regions) should map to zh-Hant. Simplified (CN/SG/Hans) → zh.
    if (primary === "zh") return /(^|-)(hant|tw|hk|mo)(-|$)/.test(lower) ? "zh-Hant" : "zh";
    // Prefer the bare primary code (e.g. "pt") over a regional variant that shares it.
    const bare = LOCALES.find((l) => l.toLowerCase() === primary);
    if (bare !== undefined) return bare;
    const byPrimary = LOCALES.find((l) => (l.toLowerCase().split("-")[0] ?? l) === primary);
    if (byPrimary !== undefined) return byPrimary;
  }
  return "en";
}

/** Load + activate a locale (English is built-in; others are lazy-loaded). Sets <html lang> + dir. */
export async function loadLocale(code: Locale): Promise<void> {
  let resolved: Locale = code;
  if (code === "en") {
    active = en;
  } else {
    const loader = loaders[code];
    if (loader === undefined) {
      active = en;
      resolved = "en";
    } else {
      try {
        active = (await loader()).default;
      } catch {
        active = en; // a missing/broken catalog must never break boot — fall back to English
        resolved = "en";
      }
    }
  }
  activeLocale = resolved;
  document.documentElement.lang = resolved;
  document.documentElement.dir = RTL.has(resolved) ? "rtl" : "ltr";
}

/** Detect the best locale from the browser's preferences, load + apply it; returns the choice. */
export async function initLocale(prefs: readonly string[]): Promise<Locale> {
  const code = pickLocale(prefs);
  await loadLocale(code);
  return code;
}
