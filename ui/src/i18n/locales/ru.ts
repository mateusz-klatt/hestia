import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} подтверждено",
  "header.unknown": "{unknown} неизвестно",

  "conn.reconnecting": "(повторное подключение…)",

  "view.rooms": "🏠 Комнаты",
  "view.back": "← Комнаты",
  "view.advanced": "🔧 Расширенные",

  "login.username": "Имя пользователя",
  "login.password": "Пароль",
  "login.submit": "Войти",
  "login.error": "✗ Неверное имя пользователя или пароль",

  "user.loggedInAs": "вход выполнен: {user}",
  "user.logout": "Выйти",
  "user.language": "Язык",
  "user.temperature": "Температура",

  "common.loading": "Загрузка…",
  "audit.title": "Журнал активности",
  "audit.empty": "Активности пока нет",
  "audit.refresh": "Обновить",
  "dbstats.title": "База данных",
  "scene.title": "Весь дом",
  "scene.lightsOff": "Выключить весь свет",
  "scene.lightsOn": "Включить весь свет",
  "scene.blindsUp": "Поднять все жалюзи",
  "scene.blindsDown": "Опустить все жалюзи",
  "rooms.empty": "Нет устройств",
  "rooms.other": "Другое",
  "rooms.deviceCount.one": "{n} устройство",
  "rooms.deviceCount.few": "{n} устройства",
  "rooms.deviceCount.many": "{n} устройств",
  "rooms.deviceCount.other": "{n} устройства",

  "ctl.on": "Вкл.",
  "ctl.off": "Выкл.",
  "ctl.raise": "Повысить",
  "ctl.lower": "Понизить",
  "ctl.set": "Установить",
  "ctl.turnOff": "Выключить",
  "ctl.sent": "✓ отправлено",
  "ctl.failed": "не удалось",
  "ctl.error": "✗ ошибка",

  "state.open": "открыто",
  "state.closed": "закрыто",
};

export default messages;
