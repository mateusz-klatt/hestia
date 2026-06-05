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
  "audit.title": "Denník aktivity",
  "audit.empty": "Zatiaľ žiadna aktivita",
  "audit.refresh": "Obnoviť",
  "rf433.title": "Zariadenia 433 MHz",
  "rf433.empty": "Zatiaľ neboli zistené žiadne zariadenia 433 MHz",
  "rf433.refresh": "Obnoviť",
  "dbstats.title": "Databáza",
  "scene.title": "Celý dom",
  "scene.lightsOff": "Zhasnúť všetky svetlá",
  "scene.lightsOn": "Rozsvietiť všetky svetlá",
  "scene.blindsUp": "Vytiahnuť všetky žalúzie",
  "scene.blindsDown": "Spustiť všetky žalúzie",
  "rooms.empty": "Žiadne zariadenia",
  "rooms.other": "Ostatné",
  "rooms.editIcons": "Upraviť ikony",
  "rooms.editIconsDone": "Hotovo",
  "rooms.iconNone": "—",
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

  "state.open": "otvorené",
  "state.closed": "zatvorené",
  "state.motion": "pohyb",
  "state.noMotion": "bez pohybu",

  "ctl.mode": "Režim",
  "ctl.brightness": "Jas",
  "klima.cool": "Chladenie",
  "klima.heat": "Kúrenie",
  "klima.auto": "Auto",
  "klima.dry": "Odvlhčovanie",
  "klima.fan": "Ventilátor",
};

export default messages;
