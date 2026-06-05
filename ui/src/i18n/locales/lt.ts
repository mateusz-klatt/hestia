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
  "audit.title": "Veiklos žurnalas",
  "audit.empty": "Veiklos dar nėra",
  "audit.refresh": "Atnaujinti",
  "rf433.title": "433 MHz įrenginiai",
  "rf433.empty": "433 MHz įrenginių dar neaptikta",
  "rf433.refresh": "Atnaujinti",
  "dbstats.title": "Duomenų bazė",
  "scene.title": "Visi namai",
  "scene.lightsOff": "Išjungti visas šviesas",
  "scene.lightsOn": "Įjungti visas šviesas",
  "scene.blindsUp": "Pakelti visas žaliuzes",
  "scene.blindsDown": "Nuleisti visas žaliuzes",
  "rooms.empty": "Nėra įrenginių",
  "rooms.other": "Kita",
  "rooms.editIcons": "Redaguoti piktogramas",
  "rooms.editIconsDone": "Atlikta",
  "rooms.iconNone": "—",
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
  "state.motion": "judesys",
  "state.noMotion": "nėra judesio",

  "ctl.mode": "Režimas",
  "ctl.brightness": "Ryškumas",
  "klima.cool": "Vėsinimas",
  "klima.heat": "Šildymas",
  "klima.auto": "Automatinis",
  "klima.dry": "Sausinimas",
  "klima.fan": "Ventiliatorius",
};

export default messages;
