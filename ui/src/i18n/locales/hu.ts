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
  "rooms.empty": "Nincsenek eszközök",
  "rooms.other": "Egyéb",
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
};

export default messages;
