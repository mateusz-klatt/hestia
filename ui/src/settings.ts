import type { UserSettings } from "./api/types";
import { currentLocale, LOCALES } from "./i18n";
import { setLocaleOverride, setTempScale, tempScale, type TempScale } from "./prefs";

const isSupportedLocale = (code: string): boolean => (LOCALES as readonly string[]).includes(code);

/**
 * Apply server-authoritative settings to the local cache once at authenticated boot; returns
 * whether anything changed (the caller reloads to apply). A change is reported ONLY when the local
 * write actually persisted, so two reload-loop traps are closed:
 *  - storage unavailable (the prefs setters return false → no change → no reload), and
 *  - an unsupported stored locale (`initLocale` would ignore it, so the effective locale could
 *    never equal it → we'd write+reload forever); we skip any locale that isn't in LOCALES.
 * (temp_scale needs no such guard — the server already rejects anything outside {C,F,K}.)
 */
export function reconcileServerSettings(settings: UserSettings | null): boolean {
  if (settings === null) return false;
  let changed = false;
  const { locale, temp_scale: scale } = settings;
  if (locale !== null && isSupportedLocale(locale) && locale !== currentLocale() && setLocaleOverride(locale)) {
    changed = true;
  }
  if (scale !== null && scale !== tempScale() && setTempScale(scale as TempScale)) changed = true;
  return changed;
}
