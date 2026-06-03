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
  "dbstats.title": "База даних",
  "rooms.empty": "Немає пристроїв",
  "rooms.other": "Інше",
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
};

export default messages;
