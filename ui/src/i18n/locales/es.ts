import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} confirmados",
  "header.unknown": "{unknown} desconocidos",

  "conn.reconnecting": "(reconectando…)",

  "view.rooms": "🏠 Habitaciones",
  "view.back": "← Habitaciones",
  "view.advanced": "🔧 Avanzado",

  "login.username": "Nombre de usuario",
  "login.password": "Contraseña",
  "login.submit": "Iniciar sesión",
  "login.error": "✗ Nombre de usuario o contraseña incorrectos",

  "user.loggedInAs": "sesión iniciada: {user}",
  "user.logout": "Cerrar sesión",
  "user.language": "Idioma",
  "user.temperature": "Temperatura",

  "common.loading": "Cargando…",
  "audit.title": "Registro de actividad",
  "audit.empty": "Aún no hay actividad",
  "audit.refresh": "Actualizar",
  "dbstats.title": "Base de datos",
  "scene.title": "Toda la casa",
  "scene.lightsOff": "Apagar todas las luces",
  "scene.lightsOn": "Encender todas las luces",
  "scene.blindsUp": "Subir todas las persianas",
  "scene.blindsDown": "Bajar todas las persianas",
  "rooms.empty": "Sin dispositivos",
  "rooms.other": "Otros",
  "rooms.editIcons": "Editar iconos",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} dispositivo",
  "rooms.deviceCount.many": "{n} dispositivos",
  "rooms.deviceCount.other": "{n} dispositivos",

  "ctl.on": "Encendido",
  "ctl.off": "Apagado",
  "ctl.raise": "Subir",
  "ctl.lower": "Bajar",
  "ctl.set": "Establecer",
  "ctl.turnOff": "Apagar",
  "ctl.sent": "✓ enviado",
  "ctl.failed": "falló",
  "ctl.error": "✗ error",

  "state.open": "abierto",
  "state.closed": "cerrado",
};

export default messages;
