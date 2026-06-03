import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} apstiprināts",
  "header.unknown": "{unknown} nezināms",

  "conn.reconnecting": "(atjauno savienojumu…)",

  "view.rooms": "🏠 Istabas",
  "view.back": "← Istabas",
  "view.advanced": "🔧 Papildu",

  "login.username": "Lietotājvārds",
  "login.password": "Parole",
  "login.submit": "Pierakstīties",
  "login.error": "✗ Nepareizs lietotājvārds vai parole",

  "user.loggedInAs": "pieteicies: {user}",
  "user.logout": "Izrakstīties",
  "user.language": "Valoda",
  "user.temperature": "Temperatūra",

  "common.loading": "Notiek ielāde…",
  "rooms.empty": "Nav ierīču",
  "rooms.other": "Citi",
  "rooms.deviceCount.zero": "{n} ierīču",
  "rooms.deviceCount.one": "{n} ierīce",
  "rooms.deviceCount.other": "{n} ierīces",

  "ctl.on": "Ieslēgts",
  "ctl.off": "Izslēgts",
  "ctl.raise": "Paaugstināt",
  "ctl.lower": "Pazemināt",
  "ctl.set": "Iestatīt",
  "ctl.turnOff": "Izslēgt",
  "ctl.sent": "✓ nosūtīts",
  "ctl.failed": "neizdevās",
  "ctl.error": "✗ kļūda",

  "state.open": "atvērts",
  "state.closed": "aizvērts",
};

export default messages;
