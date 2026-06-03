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
  "rooms.empty": "Tidak ada perangkat",
  "rooms.other": "Lainnya",
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
