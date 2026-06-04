import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} confermati",
  "header.unknown": "{unknown} sconosciuti",

  "conn.reconnecting": "(riconnessione…)",

  "view.rooms": "🏠 Stanze",
  "view.back": "← Stanze",
  "view.advanced": "🔧 Avanzate",

  "login.username": "Nome utente",
  "login.password": "Password",
  "login.submit": "Accedi",
  "login.error": "✗ Nome utente o password errati",

  "user.loggedInAs": "accesso effettuato: {user}",
  "user.logout": "Esci",
  "user.language": "Lingua",
  "user.temperature": "Temperatura",

  "common.loading": "Caricamento…",
  "audit.title": "Registro attività",
  "audit.empty": "Ancora nessuna attività",
  "audit.refresh": "Aggiorna",
  "rf433.title": "Dispositivi 433 MHz",
  "rf433.empty": "Nessun dispositivo 433 MHz rilevato finora",
  "rf433.refresh": "Aggiorna",
  "dbstats.title": "Database",
  "scene.title": "Tutta la casa",
  "scene.lightsOff": "Spegni tutte le luci",
  "scene.lightsOn": "Accendi tutte le luci",
  "scene.blindsUp": "Alza tutte le tapparelle",
  "scene.blindsDown": "Abbassa tutte le tapparelle",
  "rooms.empty": "Nessun dispositivo",
  "rooms.other": "Altro",
  "rooms.editIcons": "Modifica icone",
  "rooms.editIconsDone": "Fatto",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} dispositivo",
  "rooms.deviceCount.many": "{n} dispositivi",
  "rooms.deviceCount.other": "{n} dispositivi",

  "ctl.on": "Acceso",
  "ctl.off": "Spento",
  "ctl.raise": "Alza",
  "ctl.lower": "Abbassa",
  "ctl.set": "Imposta",
  "ctl.turnOff": "Spegni",
  "ctl.sent": "✓ inviato",
  "ctl.failed": "non riuscito",
  "ctl.error": "✗ errore",

  "state.open": "aperto",
  "state.closed": "chiuso",
};

export default messages;
