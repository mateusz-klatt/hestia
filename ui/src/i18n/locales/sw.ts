import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} vimethibitishwa",
  "header.unknown": "{unknown} visivyojulikana",

  "conn.reconnecting": "(inaunganisha tena…)",

  "view.rooms": "🏠 Vyumba",
  "view.back": "← Vyumba",
  "view.advanced": "🔧 Kina",

  "login.username": "Jina la mtumiaji",
  "login.password": "Nenosiri",
  "login.submit": "Ingia",
  "login.error": "✗ Jina la mtumiaji au nenosiri si sahihi",

  "user.loggedInAs": "umeingia: {user}",
  "user.logout": "Toka",
  "user.language": "Lugha",
  "user.temperature": "Halijoto",

  "common.loading": "Inapakia…",
  "rooms.empty": "Hakuna vifaa",
  "rooms.other": "Nyingine",
  "rooms.deviceCount.one": "{n} kifaa",
  "rooms.deviceCount.other": "{n} vifaa",

  "ctl.on": "Imewashwa",
  "ctl.off": "Imezimwa",
  "ctl.raise": "Ongeza",
  "ctl.lower": "Punguza",
  "ctl.set": "Weka",
  "ctl.turnOff": "Zima",
  "ctl.sent": "✓ imetumwa",
  "ctl.failed": "imeshindwa",
  "ctl.error": "✗ hitilafu",
};

export default messages;
