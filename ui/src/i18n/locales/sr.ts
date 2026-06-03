import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} потврђено",
  "header.unknown": "{unknown} непознато",

  "conn.reconnecting": "(поновно повезивање…)",

  "view.rooms": "🏠 Просторије",
  "view.back": "← Просторије",
  "view.advanced": "🔧 Напредно",

  "login.username": "Корисничко име",
  "login.password": "Лозинка",
  "login.submit": "Пријави се",
  "login.error": "✗ Погрешно корисничко име или лозинка",

  "user.loggedInAs": "пријављен: {user}",
  "user.logout": "Одјави се",
  "user.language": "Језик",
  "user.temperature": "Температура",

  "common.loading": "Учитавање…",
  "audit.title": "Дневник активности",
  "audit.empty": "Још нема активности",
  "audit.refresh": "Освежи",
  "dbstats.title": "База података",
  "scene.title": "Цела кућа",
  "scene.lightsOff": "Искључи сва светла",
  "scene.lightsOn": "Укључи сва светла",
  "scene.blindsUp": "Подигни све ролетне",
  "scene.blindsDown": "Спусти све ролетне",
  "rooms.empty": "Нема уређаја",
  "rooms.other": "Остало",
  "rooms.deviceCount.one": "{n} уређај",
  "rooms.deviceCount.few": "{n} уређаја",
  "rooms.deviceCount.other": "{n} уређаја",

  "ctl.on": "Укључено",
  "ctl.off": "Искључено",
  "ctl.raise": "Подигни",
  "ctl.lower": "Спусти",
  "ctl.set": "Подеси",
  "ctl.turnOff": "Искључи",
  "ctl.sent": "✓ послато",
  "ctl.failed": "није успело",
  "ctl.error": "✗ грешка",

  "state.open": "отворено",
  "state.closed": "затворено",
};

export default messages;
