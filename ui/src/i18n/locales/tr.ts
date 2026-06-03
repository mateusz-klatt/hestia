import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} onaylandı",
  "header.unknown": "{unknown} bilinmeyen",

  "conn.reconnecting": "(yeniden bağlanıyor…)",

  "view.rooms": "🏠 Odalar",
  "view.back": "← Odalar",
  "view.advanced": "🔧 Gelişmiş",

  "login.username": "Kullanıcı adı",
  "login.password": "Parola",
  "login.submit": "Oturum aç",
  "login.error": "✗ Kullanıcı adı veya parola hatalı",

  "user.loggedInAs": "oturum açıldı: {user}",
  "user.logout": "Oturumu kapat",
  "user.language": "Dil",
  "user.temperature": "Sıcaklık",

  "common.loading": "Yükleniyor…",
  "rooms.empty": "Cihaz yok",
  "rooms.other": "Diğer",
  "rooms.deviceCount.one": "{n} cihaz",
  "rooms.deviceCount.other": "{n} cihaz",

  "ctl.on": "Açık",
  "ctl.off": "Kapalı",
  "ctl.raise": "Yükselt",
  "ctl.lower": "Düşür",
  "ctl.set": "Ayarla",
  "ctl.turnOff": "Kapat",
  "ctl.sent": "✓ gönderildi",
  "ctl.failed": "başarısız",
  "ctl.error": "✗ hata",

  "state.open": "açık",
  "state.closed": "kapalı",
};

export default messages;
