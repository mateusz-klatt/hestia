import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} bekräftade",
  "header.unknown": "{unknown} okända",

  "conn.reconnecting": "(återansluter…)",

  "view.rooms": "🏠 Rum",
  "view.back": "← Rum",
  "view.advanced": "🔧 Avancerat",

  "login.username": "Användarnamn",
  "login.password": "Lösenord",
  "login.submit": "Logga in",
  "login.error": "✗ Fel användarnamn eller lösenord",

  "user.loggedInAs": "inloggad: {user}",
  "user.logout": "Logga ut",
  "user.language": "Språk",
  "user.temperature": "Temperatur",

  "common.loading": "Läser in…",
  "audit.title": "Aktivitetslogg",
  "audit.empty": "Ingen aktivitet än",
  "audit.refresh": "Uppdatera",
  "dbstats.title": "Databas",
  "scene.title": "Hela hemmet",
  "scene.lightsOff": "Släck alla lampor",
  "scene.lightsOn": "Tänd alla lampor",
  "scene.blindsUp": "Höj alla persienner",
  "scene.blindsDown": "Sänk alla persienner",
  "rooms.empty": "Inga enheter",
  "rooms.other": "Övrigt",
  "rooms.editIcons": "Redigera ikoner",
  "rooms.editIconsDone": "Klar",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} enhet",
  "rooms.deviceCount.other": "{n} enheter",

  "ctl.on": "På",
  "ctl.off": "Av",
  "ctl.raise": "Höj",
  "ctl.lower": "Sänk",
  "ctl.set": "Ställ in",
  "ctl.turnOff": "Stäng av",
  "ctl.sent": "✓ skickat",
  "ctl.failed": "misslyckades",
  "ctl.error": "✗ fel",

  "state.open": "öppen",
  "state.closed": "stängd",
};

export default messages;
