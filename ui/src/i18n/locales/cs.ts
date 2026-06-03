import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} potvrzeno",
  "header.unknown": "{unknown} neznámé",

  "conn.reconnecting": "(znovu se připojuje…)",

  "view.rooms": "🏠 Místnosti",
  "view.back": "← Místnosti",
  "view.advanced": "🔧 Pokročilé",

  "login.username": "Uživatelské jméno",
  "login.password": "Heslo",
  "login.submit": "Přihlásit se",
  "login.error": "✗ Nesprávné uživatelské jméno nebo heslo",

  "user.loggedInAs": "přihlášen: {user}",
  "user.logout": "Odhlásit se",
  "user.language": "Jazyk",
  "user.temperature": "Teplota",

  "common.loading": "Načítání…",
  "audit.title": "Protokol aktivity",
  "audit.empty": "Zatím žádná aktivita",
  "audit.refresh": "Obnovit",
  "dbstats.title": "Databáze",
  "scene.title": "Celý dům",
  "scene.lightsOff": "Zhasnout všechna světla",
  "scene.lightsOn": "Rozsvítit všechna světla",
  "scene.blindsUp": "Vytáhnout všechny žaluzie",
  "scene.blindsDown": "Spustit všechny žaluzie",
  "rooms.empty": "Žádná zařízení",
  "rooms.other": "Ostatní",
  "rooms.deviceCount.one": "{n} zařízení",
  "rooms.deviceCount.few": "{n} zařízení",
  "rooms.deviceCount.many": "{n} zařízení",
  "rooms.deviceCount.other": "{n} zařízení",

  "ctl.on": "Zapnuto",
  "ctl.off": "Vypnuto",
  "ctl.raise": "Zvýšit",
  "ctl.lower": "Snížit",
  "ctl.set": "Nastavit",
  "ctl.turnOff": "Vypnout",
  "ctl.sent": "✓ odesláno",
  "ctl.failed": "selhalo",
  "ctl.error": "✗ chyba",

  "state.open": "otevřeno",
  "state.closed": "zavřeno",
};

export default messages;
