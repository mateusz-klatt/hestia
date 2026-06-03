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
  "rooms.empty": "Gan ghléasanna",
  "rooms.other": "Eile",
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
};

export default messages;
