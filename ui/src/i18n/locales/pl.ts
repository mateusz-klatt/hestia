import type { Messages } from "./en";

// Polish — the UI's original language. Omitted keys (e.g. header.title "hestia") fall back to
// English. Polish has one/few/many plural categories (Intl.PluralRules picks per count).
const pl: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} potwierdzone",
  "header.unknown": "{unknown} nieznane",

  "conn.reconnecting": "(ponowne łączenie…)",

  "view.rooms": "🏠 Pokoje",
  "view.back": "← Pokoje",
  "view.advanced": "🔧 Zaawansowane",

  "login.username": "Użytkownik",
  "login.password": "Hasło",
  "login.submit": "Zaloguj",
  "login.error": "✗ Błędny login lub hasło",

  "user.loggedInAs": "zalogowany: {user}",
  "user.logout": "Wyloguj",
  "user.language": "Język",
  "user.temperature": "Temperatura",

  "common.loading": "Ładowanie…",
  "audit.title": "Dziennik zdarzeń",
  "audit.empty": "Brak aktywności",
  "audit.refresh": "Odśwież",
  "dbstats.title": "Baza danych",
  "scene.title": "Cały dom",
  "scene.lightsOff": "Zgaś wszystkie światła",
  "scene.lightsOn": "Włącz wszystkie światła",
  "scene.blindsUp": "Podnieś wszystkie żaluzje",
  "scene.blindsDown": "Opuść wszystkie żaluzje",
  "rooms.empty": "Brak urządzeń",
  "rooms.other": "Inne",
  "rooms.editIcons": "Edytuj ikony",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} urządzenie",
  "rooms.deviceCount.few": "{n} urządzenia",
  "rooms.deviceCount.many": "{n} urządzeń",
  "rooms.deviceCount.other": "{n} urządzenia",

  "ctl.on": "Wł",
  "ctl.off": "Wył",
  "ctl.raise": "Podnieś",
  "ctl.lower": "Opuść",
  "ctl.set": "Ustaw",
  "ctl.turnOff": "Wyłącz",
  "ctl.sent": "✓ wysłano",
  "ctl.failed": "nie powiodło się",
  "ctl.error": "✗ błąd",

  "state.open": "otwarte",
  "state.closed": "zamknięte",
};

export default pl;
