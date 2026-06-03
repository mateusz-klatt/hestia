import type { UserSettings } from "./api/types";
import { currentLocale } from "./i18n";
import { setLocaleOverride, setTempScale, tempScale, type TempScale } from "./prefs";

/**
 * Apply server-authoritative settings to the local cache once at authenticated boot; returns
 * whether anything changed (the caller reloads to apply). A change is reported ONLY when the local
 * write actually persisted: if storage is unavailable the prefs setters return false, we report no
 * change, and the boot reconcile can't reload-loop forever against a cache it is unable to update.
 */
export function reconcileServerSettings(settings: UserSettings | null): boolean {
  if (settings === null) return false;
  let changed = false;
  const { locale, temp_scale: scale } = settings;
  if (locale !== null && locale !== currentLocale() && setLocaleOverride(locale)) changed = true;
  if (scale !== null && scale !== tempScale() && setTempScale(scale as TempScale)) changed = true;
  return changed;
}
