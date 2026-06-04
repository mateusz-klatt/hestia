import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} potvrđeno",
  "header.unknown": "{unknown} nepoznato",

  "conn.reconnecting": "(ponovno povezivanje…)",

  "view.rooms": "🏠 Prostorije",
  "view.back": "← Prostorije",
  "view.advanced": "🔧 Napredno",

  "login.username": "Korisničko ime",
  "login.password": "Lozinka",
  "login.submit": "Prijava",
  "login.error": "✗ Pogrešno korisničko ime ili lozinka",

  "user.loggedInAs": "prijavljen: {user}",
  "user.logout": "Odjava",
  "user.language": "Jezik",
  "user.temperature": "Temperatura",

  "common.loading": "Učitavanje…",
  "audit.title": "Dnevnik aktivnosti",
  "audit.empty": "Još nema aktivnosti",
  "audit.refresh": "Osvježi",
  "rf433.title": "433 MHz uređaji",
  "rf433.empty": "Još nisu viđeni 433 MHz uređaji",
  "rf433.refresh": "Osvježi",
  "dbstats.title": "Baza podataka",
  "scene.title": "Cijeli dom",
  "scene.lightsOff": "Ugasi sva svjetla",
  "scene.lightsOn": "Upali sva svjetla",
  "scene.blindsUp": "Podigni sve roletne",
  "scene.blindsDown": "Spusti sve roletne",
  "rooms.empty": "Nema uređaja",
  "rooms.other": "Ostalo",
  "rooms.editIcons": "Uredi ikone",
  "rooms.editIconsDone": "Gotovo",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} uređaj",
  "rooms.deviceCount.few": "{n} uređaja",
  "rooms.deviceCount.other": "{n} uređaja",

  "ctl.on": "Uključeno",
  "ctl.off": "Isključeno",
  "ctl.raise": "Podigni",
  "ctl.lower": "Spusti",
  "ctl.set": "Postavi",
  "ctl.turnOff": "Isključi",
  "ctl.sent": "✓ poslano",
  "ctl.failed": "nije uspjelo",
  "ctl.error": "✗ greška",

  "state.open": "otvoreno",
  "state.closed": "zatvoreno",
  "state.motion": "pokret",
  "state.noMotion": "nema pokreta",
};

export default messages;
