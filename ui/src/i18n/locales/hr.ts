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
  "dbstats.title": "Baza podataka",
  "scene.title": "Cijeli dom",
  "scene.lightsOff": "Ugasi sva svjetla",
  "scene.lightsOn": "Upali sva svjetla",
  "scene.blindsUp": "Podigni sve rolete",
  "scene.blindsDown": "Spusti sve rolete",
  "rooms.empty": "Nema uređaja",
  "rooms.other": "Ostalo",
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
  "ctl.error": "✗ pogreška",

  "state.open": "otvoreno",
  "state.closed": "zatvoreno",
};

export default messages;
