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
  "audit.title": "Kumbukumbu ya shughuli",
  "audit.empty": "Hakuna shughuli bado",
  "audit.refresh": "Onyesha upya",
  "dbstats.title": "Hifadhidata",
  "scene.title": "Nyumba nzima",
  "scene.lightsOff": "Zima taa zote",
  "scene.lightsOn": "Washa taa zote",
  "scene.blindsUp": "Pandisha pazia zote",
  "scene.blindsDown": "Shusha pazia zote",
  "rooms.empty": "Hakuna vifaa",
  "rooms.other": "Nyingine",
  "rooms.editIcons": "Hariri aikoni",
  "rooms.editIconsDone": "Tayari",
  "rooms.iconNone": "—",
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

  "state.open": "wazi",
  "state.closed": "imefungwa",
};

export default messages;
