import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} confirmados",
  "header.unknown": "{unknown} desconhecidos",

  "conn.reconnecting": "(reconectando…)",

  "view.rooms": "🏠 Ambientes",
  "view.back": "← Ambientes",
  "view.advanced": "🔧 Avançado",

  "login.username": "Nome de usuário",
  "login.password": "Senha",
  "login.submit": "Entrar",
  "login.error": "✗ Nome de usuário ou senha incorretos",

  "user.loggedInAs": "conectado: {user}",
  "user.logout": "Sair",
  "user.language": "Idioma",
  "user.temperature": "Temperatura",

  "common.loading": "Carregando…",
  "audit.title": "Registro de atividade",
  "audit.empty": "Ainda sem atividade",
  "audit.refresh": "Atualizar",
  "rf433.title": "Dispositivos 433 MHz",
  "rf433.empty": "Ainda não foram detetados dispositivos 433 MHz",
  "rf433.refresh": "Atualizar",
  "dbstats.title": "Base de dados",
  "scene.title": "Toda a casa",
  "scene.lightsOff": "Apagar todas as luzes",
  "scene.lightsOn": "Acender todas as luzes",
  "scene.blindsUp": "Subir todas as persianas",
  "scene.blindsDown": "Baixar todas as persianas",
  "rooms.empty": "Sem dispositivos",
  "rooms.other": "Outros",
  "rooms.editIcons": "Editar ícones",
  "rooms.editIconsDone": "Pronto",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} dispositivo",
  "rooms.deviceCount.many": "{n} dispositivos",
  "rooms.deviceCount.other": "{n} dispositivos",

  "ctl.on": "Ligado",
  "ctl.off": "Desligado",
  "ctl.raise": "Aumentar",
  "ctl.lower": "Diminuir",
  "ctl.set": "Definir",
  "ctl.turnOff": "Desligar",
  "ctl.sent": "✓ enviado",
  "ctl.failed": "falhou",
  "ctl.error": "✗ erro",

  "state.open": "aberto",
  "state.closed": "fechado",
};

export default messages;
