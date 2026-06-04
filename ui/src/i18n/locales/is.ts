import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} staðfest",
  "header.unknown": "{unknown} óþekkt",

  "conn.reconnecting": "(tengist aftur…)",

  "view.rooms": "🏠 Herbergi",
  "view.back": "← Herbergi",
  "view.advanced": "🔧 Ítarlegt",

  "login.username": "Notandanafn",
  "login.password": "Lykilorð",
  "login.submit": "Skrá inn",
  "login.error": "✗ Rangt notandanafn eða lykilorð",

  "user.loggedInAs": "skráð inn: {user}",
  "user.logout": "Skrá út",
  "user.language": "Tungumál",
  "user.temperature": "Hitastig",

  "common.loading": "Hleður…",
  "audit.title": "Virkniskrá",
  "audit.empty": "Engin virkni enn",
  "audit.refresh": "Endurhlaða",
  "rf433.title": "433 MHz tæki",
  "rf433.empty": "Engin 433 MHz tæki hafa sést enn",
  "rf433.refresh": "Endurhlaða",
  "dbstats.title": "Gagnagrunnur",
  "scene.title": "Allt heimilið",
  "scene.lightsOff": "Slökkva á öllum ljósum",
  "scene.lightsOn": "Kveikja á öllum ljósum",
  "scene.blindsUp": "Hækka allar gardínur",
  "scene.blindsDown": "Lækka allar gardínur",
  "rooms.empty": "Engin tæki",
  "rooms.other": "Annað",
  "rooms.editIcons": "Breyta táknum",
  "rooms.editIconsDone": "Lokið",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} tæki",
  "rooms.deviceCount.other": "{n} tæki",

  "ctl.on": "Kveikt",
  "ctl.off": "Slökkt",
  "ctl.raise": "Hækka",
  "ctl.lower": "Lækka",
  "ctl.set": "Stilla",
  "ctl.turnOff": "Slökkva",
  "ctl.sent": "✓ sent",
  "ctl.failed": "mistókst",
  "ctl.error": "✗ villa",

  "state.open": "opið",
  "state.closed": "lokað",
  "state.motion": "hreyfing",
  "state.noMotion": "engin hreyfing",
};

export default messages;
