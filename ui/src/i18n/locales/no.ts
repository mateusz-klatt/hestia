import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} bekreftet",
  "header.unknown": "{unknown} ukjente",

  "conn.reconnecting": "(kobler til på nytt…)",

  "view.rooms": "🏠 Rom",
  "view.back": "← Rom",
  "view.advanced": "🔧 Avansert",

  "login.username": "Brukernavn",
  "login.password": "Passord",
  "login.submit": "Logg inn",
  "login.error": "✗ Feil brukernavn eller passord",

  "user.loggedInAs": "pålogget: {user}",
  "user.logout": "Logg ut",
  "user.language": "Språk",
  "user.temperature": "Temperatur",

  "common.loading": "Laster…",
  "audit.title": "Aktivitetslogg",
  "audit.empty": "Ingen aktivitet ennå",
  "audit.refresh": "Oppdater",
  "dbstats.title": "Database",
  "scene.title": "Hele hjemmet",
  "scene.lightsOff": "Slå av alle lys",
  "scene.lightsOn": "Slå på alle lys",
  "scene.blindsUp": "Hev alle persienner",
  "scene.blindsDown": "Senk alle persienner",
  "rooms.empty": "Ingen enheter",
  "rooms.other": "Annet",
  "rooms.deviceCount.one": "{n} enhet",
  "rooms.deviceCount.other": "{n} enheter",

  "ctl.on": "På",
  "ctl.off": "Av",
  "ctl.raise": "Hev",
  "ctl.lower": "Senk",
  "ctl.set": "Angi",
  "ctl.turnOff": "Slå av",
  "ctl.sent": "✓ sendt",
  "ctl.failed": "mislyktes",
  "ctl.error": "✗ feil",

  "state.open": "åpen",
  "state.closed": "lukket",
};

export default messages;
