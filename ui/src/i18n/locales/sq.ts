import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} të konfirmuara",
  "header.unknown": "{unknown} të panjohura",

  "conn.reconnecting": "(po rilidhet…)",

  "view.rooms": "🏠 Dhomat",
  "view.back": "← Dhomat",
  "view.advanced": "🔧 Të avancuara",

  "login.username": "Emri i përdoruesit",
  "login.password": "Fjalëkalimi",
  "login.submit": "Hyr",
  "login.error": "✗ Emri i përdoruesit ose fjalëkalimi është i gabuar",

  "user.loggedInAs": "i identifikuar: {user}",
  "user.logout": "Dil",
  "user.language": "Gjuha",
  "user.temperature": "Temperatura",

  "common.loading": "Duke u ngarkuar…",
  "audit.title": "Ditari i aktivitetit",
  "audit.empty": "Ende nuk ka aktivitet",
  "audit.refresh": "Rifresko",
  "dbstats.title": "Baza e të dhënave",
  "scene.title": "E gjithë shtëpia",
  "scene.lightsOff": "Fik të gjitha dritat",
  "scene.lightsOn": "Ndiz të gjitha dritat",
  "scene.blindsUp": "Ngri të gjitha grilat",
  "scene.blindsDown": "Uli të gjitha grilat",
  "rooms.empty": "Nuk ka pajisje",
  "rooms.other": "Të tjera",
  "rooms.deviceCount.one": "{n} pajisje",
  "rooms.deviceCount.other": "{n} pajisje",

  "ctl.on": "Ndezur",
  "ctl.off": "Fikur",
  "ctl.raise": "Ngri",
  "ctl.lower": "Ul",
  "ctl.set": "Vendos",
  "ctl.turnOff": "Fik",
  "ctl.sent": "✓ u dërgua",
  "ctl.failed": "dështoi",
  "ctl.error": "✗ gabim",

  "state.open": "hapur",
  "state.closed": "mbyllur",
};

export default messages;
