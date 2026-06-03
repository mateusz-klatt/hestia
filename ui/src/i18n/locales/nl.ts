import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} bevestigd",
  "header.unknown": "{unknown} onbekend",

  "conn.reconnecting": "(opnieuw verbinden…)",

  "view.rooms": "🏠 Kamers",
  "view.back": "← Kamers",
  "view.advanced": "🔧 Geavanceerd",

  "login.username": "Gebruikersnaam",
  "login.password": "Wachtwoord",
  "login.submit": "Aanmelden",
  "login.error": "✗ Onjuiste gebruikersnaam of onjuist wachtwoord",

  "user.loggedInAs": "aangemeld: {user}",
  "user.logout": "Afmelden",
  "user.language": "Taal",
  "user.temperature": "Temperatuur",

  "common.loading": "Laden…",
  "rooms.empty": "Geen apparaten",
  "rooms.other": "Overig",
  "rooms.deviceCount.one": "{n} apparaat",
  "rooms.deviceCount.other": "{n} apparaten",

  "ctl.on": "Aan",
  "ctl.off": "Uit",
  "ctl.raise": "Hoger",
  "ctl.lower": "Lager",
  "ctl.set": "Instellen",
  "ctl.turnOff": "Uitschakelen",
  "ctl.sent": "✓ verzonden",
  "ctl.failed": "mislukt",
  "ctl.error": "✗ fout",
};

export default messages;
