import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} potvrdené",
  "header.unknown": "{unknown} neznáme",

  "conn.reconnecting": "(opätovné pripájanie…)",

  "view.rooms": "🏠 Miestnosti",
  "view.back": "← Miestnosti",
  "view.advanced": "🔧 Pokročilé",

  "login.username": "Používateľské meno",
  "login.password": "Heslo",
  "login.submit": "Prihlásiť sa",
  "login.error": "✗ Nesprávne používateľské meno alebo heslo",

  "user.loggedInAs": "prihlásený: {user}",
  "user.logout": "Odhlásiť sa",
  "user.language": "Jazyk",
  "user.temperature": "Teplota",

  "common.loading": "Načítava sa…",
  "rooms.empty": "Žiadne zariadenia",
  "rooms.other": "Ostatné",
  "rooms.deviceCount.one": "{n} zariadenie",
  "rooms.deviceCount.few": "{n} zariadenia",
  "rooms.deviceCount.many": "{n} zariadení",
  "rooms.deviceCount.other": "{n} zariadenia",

  "ctl.on": "Zapnuté",
  "ctl.off": "Vypnuté",
  "ctl.raise": "Zvýšiť",
  "ctl.lower": "Znížiť",
  "ctl.set": "Nastaviť",
  "ctl.turnOff": "Vypnúť",
  "ctl.sent": "✓ odoslané",
  "ctl.failed": "zlyhalo",
  "ctl.error": "✗ chyba",
};

export default messages;
