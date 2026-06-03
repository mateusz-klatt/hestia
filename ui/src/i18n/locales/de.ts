import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} bestätigt",
  "header.unknown": "{unknown} unbekannt",

  "conn.reconnecting": "(Verbindung wird wiederhergestellt…)",

  "view.rooms": "🏠 Räume",
  "view.back": "← Räume",
  "view.advanced": "🔧 Erweitert",

  "login.username": "Benutzername",
  "login.password": "Passwort",
  "login.submit": "Anmelden",
  "login.error": "✗ Falscher Benutzername oder falsches Passwort",

  "user.loggedInAs": "angemeldet: {user}",
  "user.logout": "Abmelden",
  "user.language": "Sprache",
  "user.temperature": "Temperatur",

  "common.loading": "Lädt…",
  "rooms.empty": "Keine Geräte",
  "rooms.other": "Sonstige",
  "rooms.deviceCount.one": "{n} Gerät",
  "rooms.deviceCount.other": "{n} Geräte",

  "ctl.on": "Ein",
  "ctl.off": "Aus",
  "ctl.raise": "Erhöhen",
  "ctl.lower": "Verringern",
  "ctl.set": "Setzen",
  "ctl.turnOff": "Ausschalten",
  "ctl.sent": "✓ gesendet",
  "ctl.failed": "fehlgeschlagen",
  "ctl.error": "✗ Fehler",
};

export default messages;
