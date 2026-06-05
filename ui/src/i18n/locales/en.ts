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
  // Audit feed: humanized at display time (the backend stores raw facts). Actors —
  "audit.actor.device": "Device",
  "audit.actor.system": "System",
  "audit.actor.anonymous": "Anonymous",
  "audit.actor.automation": "Automation: {id}",
  // Actions (the full fixed vocabulary; an unknown action falls back to its raw code) —
  "audit.action.switch": "Switch",
  "audit.action.level": "Level",
  "audit.action.cover": "Blind position",
  "audit.action.thermostat": "Set temperature",
  "audit.action.thermostat_power": "Thermostat power",
  "audit.action.ir": "IR signal",
  "audit.action.name": "Rename",
  "audit.action.scene": "Scene",
  "audit.action.graduate": "Standalone mode",
  "audit.action.automation_set": "Automation saved",
  "audit.action.automation_delete": "Automation deleted",
  "audit.action.settings": "Settings updated",
  "audit.action.room_icon": "Room icon",
  "audit.action.login": "Login",
  "audit.action.door": "Door state",
  "audit.action.endpoints": "Channels",
  "audit.action.thermostat_on": "Thermostat state",
  "audit.action.setpoint": "Setpoint",
  // Results (known enums; error:<msg> and <sent>/<total> stay raw) —
  "audit.result.ok": "OK",
  "audit.result.invalid": "Invalid",
  "audit.result.reported": "Reported",
  "audit.result.fired": "Fired",
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
  "state.raised": "raised",
  "state.lowered": "lowered",

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

  // Engineer (Advanced) device table — column headers + cell labels.
  "tbl.node": "node",
  "tbl.lastSeen": "last seen",
  "tbl.battery": "battery",
  "tbl.inferredType": "inferred type",
  "tbl.state": "state",
  "tbl.actions": "actions",
  "tbl.name": "name",
  "tbl.room": "room",
  "dev.confirm": "✓ confirm",
  "dev.save": "Save",
  "dev.battLow": "low",
  "dev.channel": "↳ channel {ep}",

  // Device type names (the classifier's values), shown in the "inferred type" cell + as a fallback label.
  "type.light": "light",
  "type.blind": "blind",
  "type.thermostat": "thermostat",
  "type.plug": "plug",
  "type.motion": "motion",
  "type.door": "door",
  "type.water": "water",
  "type.smoke": "smoke",
  "type.unknown": "unknown",

  // Running-mode status line ("mode: …"): standalone is cloud-free; an env override or a saved-but-not-
  // applied graduation each get a note. {mode}/{target}/{override} are the raw mode names.
  "mode.plain": "mode: {mode}",
  "mode.cloudFree": "mode: {mode} (cloud-free)",
  "mode.graduated": "mode: {mode} → standalone saved — restart hestia",
  "mode.envPinned": "mode: {mode} (HESTIA_MODE={override} forces the mode; saved: {target})",
  "discovery.loadError": "could not load /api/discovery",
  "adv.refresh": "Refresh",

  // Automations editor (Advanced) — table headers, row actions, editor buttons.
  "auto.title": "automations",
  "auto.id": "id",
  "auto.enabled": "enabled",
  "auto.trigger": "trigger",
  "auto.conditions": "conditions",
  "auto.actions": "actions",
  "auto.edit": "Edit",
  "auto.delete": "Delete",
  "auto.deleteConfirm": "Delete rule {id}?",
  "auto.unavailable": "(automations unavailable)",
  "rule.newTemplate": "New rule template",
  "rule.save": "Save rule",
  "rule.jsonLabel": "rule JSON",
  "rule.jsonPlaceholder": "rule JSON — click \"New rule template\" for a skeleton",
  "rule.editing": "editing {id}",
  "rule.templateLoaded": "template loaded — edit then Save",
  "rule.invalidJson": "invalid JSON: {msg}",
  "rule.saved": "saved",
  "rule.error": "error {status}",
};

export default en;
export type Messages = typeof en;
export type MessageKey = keyof Messages;
