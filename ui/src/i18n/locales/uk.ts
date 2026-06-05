import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} підтверджено",
  "header.unknown": "{unknown} невідомо",

  "conn.reconnecting": "(повторне підключення…)",

  "view.rooms": "🏠 Кімнати",
  "view.back": "← Кімнати",
  "view.advanced": "🔧 Розширені",

  "login.username": "Ім’я користувача",
  "login.password": "Пароль",
  "login.submit": "Увійти",
  "login.error": "✗ Неправильне ім’я користувача або пароль",

  "user.loggedInAs": "вхід виконано: {user}",
  "user.logout": "Вийти",
  "user.language": "Мова",
  "user.temperature": "Температура",

  "common.loading": "Завантаження…",
  "audit.title": "Журнал активності",
  "audit.empty": "Активності ще немає",
  "audit.refresh": "Оновити",
  "rf433.title": "Пристрої 433 MHz",
  "rf433.empty": "Пристрої 433 MHz ще не виявлено",
  "rf433.refresh": "Оновити",
  "dbstats.title": "База даних",
  "scene.title": "Весь дім",
  "scene.lightsOff": "Вимкнути все світло",
  "scene.lightsOn": "Увімкнути все світло",
  "scene.blindsUp": "Підняти всі жалюзі",
  "scene.blindsDown": "Опустити всі жалюзі",
  "rooms.empty": "Немає пристроїв",
  "rooms.other": "Інше",
  "rooms.editIcons": "Редагувати піктограми",
  "rooms.editIconsDone": "Готово",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} пристрій",
  "rooms.deviceCount.few": "{n} пристрої",
  "rooms.deviceCount.many": "{n} пристроїв",
  "rooms.deviceCount.other": "{n} пристрою",

  "ctl.on": "Увімкнено",
  "ctl.off": "Вимкнено",
  "ctl.raise": "Підняти",
  "ctl.lower": "Опустити",
  "ctl.set": "Встановити",
  "ctl.turnOff": "Вимкнути",
  "ctl.sent": "✓ надіслано",
  "ctl.failed": "не вдалося",
  "ctl.error": "✗ помилка",

  "state.open": "відчинено",
  "state.closed": "зачинено",
  "state.motion": "рух",
  "state.noMotion": "немає руху",

  "ctl.mode": "Режим",
  "ctl.brightness": "Яскравість",
  "klima.cool": "Охолодження",
  "klima.heat": "Обігрів",
  "klima.auto": "Авто",
  "klima.dry": "Осушення",
  "klima.fan": "Вентилятор",
};

export default messages;
