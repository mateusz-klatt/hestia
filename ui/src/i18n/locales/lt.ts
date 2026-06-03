import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} patvirtinta",
  "header.unknown": "{unknown} nežinoma",

  "conn.reconnecting": "(jungiamasi iš naujo…)",

  "view.rooms": "🏠 Kambariai",
  "view.back": "← Kambariai",
  "view.advanced": "🔧 Išplėstiniai",

  "login.username": "Naudotojo vardas",
  "login.password": "Slaptažodis",
  "login.submit": "Prisijungti",
  "login.error": "✗ Neteisingas naudotojo vardas arba slaptažodis",

  "user.loggedInAs": "prisijungta: {user}",
  "user.logout": "Atsijungti",
  "user.language": "Kalba",
  "user.temperature": "Temperatūra",

  "common.loading": "Įkeliama…",
  "rooms.empty": "Nėra įrenginių",
  "rooms.other": "Kita",
  "rooms.deviceCount.one": "{n} įrenginys",
  "rooms.deviceCount.few": "{n} įrenginiai",
  "rooms.deviceCount.many": "{n} įrenginių",
  "rooms.deviceCount.other": "{n} įrenginio",

  "ctl.on": "Įjungta",
  "ctl.off": "Išjungta",
  "ctl.raise": "Pakelkite",
  "ctl.lower": "Nuleiskite",
  "ctl.set": "Nustatyti",
  "ctl.turnOff": "Išjungti",
  "ctl.sent": "✓ išsiųsta",
  "ctl.failed": "nepavyko",
  "ctl.error": "✗ klaida",

  "state.open": "atidaryta",
  "state.closed": "uždaryta",
};

export default messages;
