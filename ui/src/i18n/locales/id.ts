import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} dikonfirmasi",
  "header.unknown": "{unknown} tidak diketahui",

  "conn.reconnecting": "(menghubungkan ulang…)",

  "view.rooms": "🏠 Ruangan",
  "view.back": "← Ruangan",
  "view.advanced": "🔧 Lanjutan",

  "login.username": "Nama pengguna",
  "login.password": "Kata sandi",
  "login.submit": "Masuk",
  "login.error": "✗ Nama pengguna atau kata sandi salah",

  "user.loggedInAs": "masuk sebagai: {user}",
  "user.logout": "Keluar",
  "user.language": "Bahasa",
  "user.temperature": "Suhu",

  "common.loading": "Memuat…",
  "audit.title": "Log aktivitas",
  "audit.empty": "Belum ada aktivitas",
  "audit.refresh": "Segarkan",
  "dbstats.title": "Basis data",
  "scene.title": "Seluruh rumah",
  "scene.lightsOff": "Matikan semua lampu",
  "scene.lightsOn": "Nyalakan semua lampu",
  "scene.blindsUp": "Naikkan semua tirai",
  "scene.blindsDown": "Turunkan semua tirai",
  "rooms.empty": "Tidak ada perangkat",
  "rooms.other": "Lainnya",
  "rooms.editIcons": "Edit ikon",
  "rooms.editIconsDone": "Selesai",
  "rooms.iconNone": "—",
  "rooms.deviceCount.other": "{n} perangkat",

  "ctl.on": "Nyala",
  "ctl.off": "Mati",
  "ctl.raise": "Naikkan",
  "ctl.lower": "Turunkan",
  "ctl.set": "Atur",
  "ctl.turnOff": "Matikan",
  "ctl.sent": "✓ terkirim",
  "ctl.failed": "gagal",
  "ctl.error": "✗ kesalahan",

  "state.open": "terbuka",
  "state.closed": "tertutup",
};

export default messages;
