import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} disahkan",
  "header.unknown": "{unknown} tidak diketahui",

  "conn.reconnecting": "(menyambung semula…)",

  "view.rooms": "🏠 Bilik",
  "view.back": "← Bilik",
  "view.advanced": "🔧 Lanjutan",

  "login.username": "Nama pengguna",
  "login.password": "Kata laluan",
  "login.submit": "Log masuk",
  "login.error": "✗ Nama pengguna atau kata laluan salah",

  "user.loggedInAs": "log masuk: {user}",
  "user.logout": "Log keluar",
  "user.language": "Bahasa",
  "user.temperature": "Suhu",

  "common.loading": "Memuatkan…",
  "rooms.empty": "Tiada peranti",
  "rooms.other": "Lain-lain",
  "rooms.deviceCount.other": "{n} peranti",

  "ctl.on": "Hidup",
  "ctl.off": "Mati",
  "ctl.raise": "Naikkan",
  "ctl.lower": "Turunkan",
  "ctl.set": "Tetapkan",
  "ctl.turnOff": "Matikan",
  "ctl.sent": "✓ dihantar",
  "ctl.failed": "gagal",
  "ctl.error": "✗ ralat",

  "state.open": "terbuka",
  "state.closed": "tertutup",
};

export default messages;
