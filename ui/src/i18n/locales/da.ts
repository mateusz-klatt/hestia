import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} bekræftet",
  "header.unknown": "{unknown} ukendte",

  "conn.reconnecting": "(genopretter forbindelse…)",

  "view.rooms": "🏠 Rum",
  "view.back": "← Rum",
  "view.advanced": "🔧 Avanceret",

  "login.username": "Brugernavn",
  "login.password": "Adgangskode",
  "login.submit": "Log ind",
  "login.error": "✗ Forkert brugernavn eller adgangskode",

  "user.loggedInAs": "logget ind: {user}",
  "user.logout": "Log ud",
  "user.language": "Sprog",
  "user.temperature": "Temperatur",

  "common.loading": "Indlæser…",
  "audit.title": "Aktivitetslog",
  "audit.empty": "Ingen aktivitet endnu",
  "audit.refresh": "Opdater",
  "rf433.title": "433 MHz-enheder",
  "rf433.empty": "Ingen 433 MHz-enheder set endnu",
  "rf433.refresh": "Opdater",
  "dbstats.title": "Database",
  "scene.title": "Hele hjemmet",
  "scene.lightsOff": "Sluk alle lys",
  "scene.lightsOn": "Tænd alle lys",
  "scene.blindsUp": "Hæv alle persienner",
  "scene.blindsDown": "Sænk alle persienner",
  "rooms.empty": "Ingen enheder",
  "rooms.other": "Andet",
  "rooms.editIcons": "Rediger ikoner",
  "rooms.editIconsDone": "Færdig",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} enhed",
  "rooms.deviceCount.other": "{n} enheder",

  "ctl.on": "Til",
  "ctl.off": "Fra",
  "ctl.raise": "Hæv",
  "ctl.lower": "Sænk",
  "ctl.set": "Indstil",
  "ctl.turnOff": "Sluk",
  "ctl.sent": "✓ sendt",
  "ctl.failed": "mislykkedes",
  "ctl.error": "✗ fejl",

  "state.open": "åben",
  "state.closed": "lukket",
  "state.motion": "bevægelse",
  "state.noMotion": "ingen bevægelse",

  "ctl.mode": "Tilstand",
  "ctl.brightness": "Lysstyrke",
  "klima.cool": "Køling",
  "klima.heat": "Varme",
  "klima.auto": "Auto",
  "klima.dry": "Affugtning",
  "klima.fan": "Ventilator",
};

export default messages;
