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
  "view.events": "📜 Activity",
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
  "rf433.title": "433 MHz devices",
  "rf433.empty": "No 433 MHz devices seen yet",
  "rf433.refresh": "Refresh",
  "dbstats.title": "Database",
  "scene.title": "Whole home",
  "scene.lightsOff": "All lights off",
  "scene.lightsOn": "All lights on",
  "scene.blindsUp": "Raise all blinds",
  "scene.blindsDown": "Lower all blinds",
  "rooms.empty": "No devices",
  "rooms.other": "Other",
  "rooms.editIcons": "Edit icons",
  "rooms.editIconsDone": "Done",
  "rooms.iconNone": "—",
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
  "state.motion": "motion",
  "state.noMotion": "no motion",

  // Accessible names for the icon-only control <select> dropdowns (screen-reader only).
  "ctl.mode": "Mode",
  "ctl.brightness": "Brightness",

  // A/C (klima) mode option labels — the dropdown VALUE stays the raw mode (it keys the IR signal
  // on_<mode>_<temp>); only the visible label is localised. Unknown modes fall back to the raw name.
  "klima.cool": "Cool",
  "klima.heat": "Heat",
  "klima.auto": "Auto",
  "klima.dry": "Dry",
  "klima.fan": "Fan",
};

export default en;
export type Messages = typeof en;
export type MessageKey = keyof Messages;
