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
  "audit.title": "Darbību žurnāls",
  "audit.empty": "Pagaidām nav darbību",
  "audit.refresh": "Atsvaidzināt",
  "rf433.title": "433 MHz ierīces",
  "rf433.empty": "433 MHz ierīces vēl nav konstatētas",
  "rf433.refresh": "Atsvaidzināt",
  "dbstats.title": "Datubāze",
  "scene.title": "Visa māja",
  "scene.lightsOff": "Izslēgt visas gaismas",
  "scene.lightsOn": "Ieslēgt visas gaismas",
  "scene.blindsUp": "Pacelt visas žalūzijas",
  "scene.blindsDown": "Nolaist visas žalūzijas",
  "rooms.empty": "Nav ierīču",
  "rooms.other": "Citi",
  "rooms.editIcons": "Rediģēt ikonas",
  "rooms.editIconsDone": "Gatavs",
  "rooms.iconNone": "—",
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
