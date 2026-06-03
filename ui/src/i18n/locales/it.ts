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
  "rooms.empty": "Nessun dispositivo",
  "rooms.other": "Altro",
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
