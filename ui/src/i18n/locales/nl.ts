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
  "audit.title": "Activiteitenlogboek",
  "audit.empty": "Nog geen activiteit",
  "audit.refresh": "Vernieuwen",
  "rf433.title": "433 MHz-apparaten",
  "rf433.empty": "Nog geen 433 MHz-apparaten gezien",
  "rf433.refresh": "Vernieuwen",
  "dbstats.title": "Database",
  "scene.title": "Hele huis",
  "scene.lightsOff": "Alle lichten uit",
  "scene.lightsOn": "Alle lichten aan",
  "scene.blindsUp": "Alle jaloezieën omhoog",
  "scene.blindsDown": "Alle jaloezieën omlaag",
  "rooms.empty": "Geen apparaten",
  "rooms.other": "Overig",
  "rooms.editIcons": "Pictogrammen bewerken",
  "rooms.editIconsDone": "Klaar",
  "rooms.iconNone": "—",
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

  "state.open": "open",
  "state.closed": "gesloten",
};

export default messages;
