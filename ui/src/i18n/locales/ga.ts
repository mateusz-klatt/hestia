import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} dearbhaithe",
  "header.unknown": "{unknown} anaithnid",

  "conn.reconnecting": "(ag athcheangal…)",

  "view.rooms": "🏠 Seomraí",
  "view.back": "← Seomraí",
  "view.advanced": "🔧 Ardroghanna",

  "login.username": "Ainm úsáideora",
  "login.password": "Pasfhocal",
  "login.submit": "Sínigh isteach",
  "login.error": "✗ Ainm úsáideora nó pasfhocal mícheart",

  "user.loggedInAs": "sínithe isteach: {user}",
  "user.logout": "Sínigh amach",
  "user.language": "Teanga",
  "user.temperature": "Teocht",

  "common.loading": "Á lódáil…",
  "audit.title": "Log gníomhaíochta",
  "audit.empty": "Níl aon ghníomhaíocht fós",
  "audit.refresh": "Athnuaigh",
  "rf433.title": "Gléasanna 433 MHz",
  "rf433.empty": "Níor braitheadh gléasanna 433 MHz fós",
  "rf433.refresh": "Athnuaigh",
  "dbstats.title": "Bunachar sonraí",
  "scene.title": "An teach ar fad",
  "scene.lightsOff": "Múch gach solas",
  "scene.lightsOn": "Las gach solas",
  "scene.blindsUp": "Ardaigh gach dallóg",
  "scene.blindsDown": "Ísligh gach dallóg",
  "rooms.empty": "Gan ghléasanna",
  "rooms.other": "Eile",
  "rooms.editIcons": "Cuir deilbhíní in eagar",
  "rooms.editIconsDone": "Déanta",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} ghléas",
  "rooms.deviceCount.two": "{n} ghléas",
  "rooms.deviceCount.few": "{n} ghléas",
  "rooms.deviceCount.many": "{n} ngléas",
  "rooms.deviceCount.other": "{n} gléas",

  "ctl.on": "Ar siúl",
  "ctl.off": "As",
  "ctl.raise": "Ardaigh",
  "ctl.lower": "Ísligh",
  "ctl.set": "Socraigh",
  "ctl.turnOff": "Múch",
  "ctl.sent": "✓ seolta",
  "ctl.failed": "theip",
  "ctl.error": "✗ earráid",

  "state.open": "oscailte",
  "state.closed": "dúnta",
  "state.motion": "gluaiseacht",
  "state.noMotion": "gan ghluaiseacht",

  "ctl.mode": "Mód",
  "ctl.brightness": "Gile",
  "klima.cool": "Fuarú",
  "klima.heat": "Téamh",
  "klima.auto": "Uathoibríoch",
  "klima.dry": "Díthaisiú",
  "klima.fan": "Gaothrán",
};

export default messages;
