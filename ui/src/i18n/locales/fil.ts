import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} nakumpirma",
  "header.unknown": "{unknown} hindi kilala",

  "conn.reconnecting": "(muling kumokonekta…)",

  "view.rooms": "🏠 Mga kuwarto",
  "view.back": "← Mga kuwarto",
  "view.advanced": "🔧 Advanced",

  "login.username": "Username",
  "login.password": "Password",
  "login.submit": "Mag-sign in",
  "login.error": "✗ Mali ang username o password",

  "user.loggedInAs": "naka-sign in: {user}",
  "user.logout": "Mag-log out",
  "user.language": "Wika",
  "user.temperature": "Temperatura",

  "common.loading": "Naglo-load…",
  "audit.title": "Log ng aktibidad",
  "audit.empty": "Wala pang aktibidad",
  "audit.refresh": "I-refresh",
  "rf433.title": "Mga 433 MHz device",
  "rf433.empty": "Wala pang nakikitang 433 MHz device",
  "rf433.refresh": "I-refresh",
  "dbstats.title": "Database",
  "scene.title": "Buong tahanan",
  "scene.lightsOff": "Patayin ang lahat ng ilaw",
  "scene.lightsOn": "Buksan ang lahat ng ilaw",
  "scene.blindsUp": "Itaas ang lahat ng blinds",
  "scene.blindsDown": "Ibaba ang lahat ng blinds",
  "rooms.empty": "Walang device",
  "rooms.other": "Iba pa",
  "rooms.editIcons": "I-edit ang mga icon",
  "rooms.editIconsDone": "Tapos",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} device",
  "rooms.deviceCount.other": "{n} device",

  "ctl.on": "Naka-on",
  "ctl.off": "Naka-off",
  "ctl.raise": "Itaas",
  "ctl.lower": "Ibaba",
  "ctl.set": "Itakda",
  "ctl.turnOff": "I-off",
  "ctl.sent": "✓ naipadala",
  "ctl.failed": "nabigo",
  "ctl.error": "✗ error",

  "state.open": "bukas",
  "state.closed": "sarado",
  "state.motion": "galaw",
  "state.noMotion": "walang galaw",

  "ctl.mode": "Mode",
  "ctl.brightness": "Liwanag",
  "klima.cool": "Lamig",
  "klima.heat": "Init",
  "klima.auto": "Auto",
  "klima.dry": "Patuyo",
  "klima.fan": "Bentilador",
};

export default messages;
