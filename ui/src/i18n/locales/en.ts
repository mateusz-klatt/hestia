// English is the SOURCE catalog: every message key lives here. Other locales override a
// subset and fall back to English for anything missing. Plural keys declare all CLDR
// categories (zero/one/two/few/many/other) so any language can fill the ones it uses —
// the active category is chosen at runtime via Intl.PluralRules (see tPlural).
const en = {
  "header.title": "hestia",
  "header.confirmed": "{confirmed}/{total} confirmed",
  "header.unknown": "{unknown} unknown",

  "conn.reconnecting": "(reconnecting…)",

  "view.rooms": "🏠 Rooms",
  "view.back": "← Rooms",
  "view.advanced": "🔧 Advanced",

  "login.username": "Username",
  "login.password": "Password",
  "login.submit": "Sign in",
  "login.error": "✗ Wrong username or password",

  "user.loggedInAs": "signed in: {user}",
  "user.logout": "Log out",
  "user.language": "Language",
  "user.temperature": "Temperature",

  "common.loading": "Loading…",
  "audit.title": "Activity log",
  "audit.empty": "No activity yet",
  "audit.refresh": "Refresh",
  "rooms.empty": "No devices",
  "rooms.other": "Other",
  "rooms.deviceCount.zero": "{n} devices",
  "rooms.deviceCount.one": "{n} device",
  "rooms.deviceCount.two": "{n} devices",
  "rooms.deviceCount.few": "{n} devices",
  "rooms.deviceCount.many": "{n} devices",
  "rooms.deviceCount.other": "{n} devices",

  "ctl.on": "On",
  "ctl.off": "Off",
  "ctl.raise": "Raise",
  "ctl.lower": "Lower",
  "ctl.set": "Set",
  "ctl.turnOff": "Turn off",
  "ctl.sent": "✓ sent",
  "ctl.failed": "failed",
  "ctl.error": "✗ error",

  "state.open": "open",
  "state.closed": "closed",
};

export default en;
export type Messages = typeof en;
export type MessageKey = keyof Messages;
