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
};

export default messages;
