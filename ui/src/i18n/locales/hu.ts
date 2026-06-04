import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} megerősítve",
  "header.unknown": "{unknown} ismeretlen",

  "conn.reconnecting": "(újracsatlakozás…)",

  "view.rooms": "🏠 Helyiségek",
  "view.back": "← Helyiségek",
  "view.advanced": "🔧 Speciális",

  "login.username": "Felhasználónév",
  "login.password": "Jelszó",
  "login.submit": "Bejelentkezés",
  "login.error": "✗ Hibás felhasználónév vagy jelszó",

  "user.loggedInAs": "bejelentkezve: {user}",
  "user.logout": "Kijelentkezés",
  "user.language": "Nyelv",
  "user.temperature": "Hőmérséklet",

  "common.loading": "Betöltés…",
  "audit.title": "Tevékenységnapló",
  "audit.empty": "Még nincs tevékenység",
  "audit.refresh": "Frissítés",
  "rf433.title": "433 MHz-es eszközök",
  "rf433.empty": "Még nem láthatók 433 MHz-es eszközök",
  "rf433.refresh": "Frissítés",
  "dbstats.title": "Adatbázis",
  "scene.title": "Teljes otthon",
  "scene.lightsOff": "Minden lámpa ki",
  "scene.lightsOn": "Minden lámpa be",
  "scene.blindsUp": "Minden redőny fel",
  "scene.blindsDown": "Minden redőny le",
  "rooms.empty": "Nincsenek eszközök",
  "rooms.other": "Egyéb",
  "rooms.editIcons": "Ikonok szerkesztése",
  "rooms.editIconsDone": "Kész",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} eszköz",
  "rooms.deviceCount.other": "{n} eszköz",

  "ctl.on": "Be",
  "ctl.off": "Ki",
  "ctl.raise": "Emelés",
  "ctl.lower": "Csökkentés",
  "ctl.set": "Beállítás",
  "ctl.turnOff": "Kikapcsolás",
  "ctl.sent": "✓ elküldve",
  "ctl.failed": "sikertelen",
  "ctl.error": "✗ hiba",

  "state.open": "nyitva",
  "state.closed": "zárva",
};

export default messages;
