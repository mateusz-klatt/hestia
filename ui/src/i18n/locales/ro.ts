import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} confirmate",
  "header.unknown": "{unknown} necunoscute",

  "conn.reconnecting": "(reconectare…)",

  "view.rooms": "🏠 Camere",
  "view.back": "← Camere",
  "view.advanced": "🔧 Avansat",

  "login.username": "Nume de utilizator",
  "login.password": "Parolă",
  "login.submit": "Autentificare",
  "login.error": "✗ Nume de utilizator sau parolă greșite",

  "user.loggedInAs": "autentificat: {user}",
  "user.logout": "Deconectare",
  "user.language": "Limbă",
  "user.temperature": "Temperatură",

  "common.loading": "Se încarcă…",
  "audit.title": "Jurnal de activitate",
  "audit.empty": "Nicio activitate încă",
  "audit.refresh": "Reîmprospătează",
  "dbstats.title": "Bază de date",
  "scene.title": "Toată casa",
  "scene.lightsOff": "Stinge toate luminile",
  "scene.lightsOn": "Aprinde toate luminile",
  "scene.blindsUp": "Ridică toate jaluzelele",
  "scene.blindsDown": "Coboară toate jaluzelele",
  "rooms.empty": "Niciun dispozitiv",
  "rooms.other": "Altele",
  "rooms.editIcons": "Editați pictogramele",
  "rooms.editIconsDone": "Gata",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} dispozitiv",
  "rooms.deviceCount.few": "{n} dispozitive",
  "rooms.deviceCount.other": "{n} dispozitive",

  "ctl.on": "Pornit",
  "ctl.off": "Oprit",
  "ctl.raise": "Ridică",
  "ctl.lower": "Coboară",
  "ctl.set": "Setează",
  "ctl.turnOff": "Oprește",
  "ctl.sent": "✓ trimis",
  "ctl.failed": "eșuat",
  "ctl.error": "✗ eroare",

  "state.open": "deschis",
  "state.closed": "închis",
};

export default messages;
