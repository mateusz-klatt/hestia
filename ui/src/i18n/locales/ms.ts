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
  "audit.title": "Log aktiviti",
  "audit.empty": "Belum ada aktiviti",
  "audit.refresh": "Segar semula",
  "rf433.title": "Peranti 433 MHz",
  "rf433.empty": "Belum ada peranti 433 MHz dikesan",
  "rf433.refresh": "Segar semula",
  "dbstats.title": "Pangkalan data",
  "scene.title": "Seluruh rumah",
  "scene.lightsOff": "Matikan semua lampu",
  "scene.lightsOn": "Hidupkan semua lampu",
  "scene.blindsUp": "Naikkan semua bidai",
  "scene.blindsDown": "Turunkan semua bidai",
  "rooms.empty": "Tiada peranti",
  "rooms.other": "Lain-lain",
  "rooms.editIcons": "Edit ikon",
  "rooms.editIconsDone": "Selesai",
  "rooms.iconNone": "—",
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
  "state.motion": "gerakan",
  "state.noMotion": "tiada gerakan",

  "ctl.mode": "Mod",
  "ctl.brightness": "Kecerahan",
  "klima.cool": "Sejuk",
  "klima.heat": "Panas",
  "klima.auto": "Auto",
  "klima.dry": "Kering",
  "klima.fan": "Kipas",
};

export default messages;
