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
  "audit.title": "Etkinlik günlüğü",
  "audit.empty": "Henüz etkinlik yok",
  "audit.refresh": "Yenile",
  "rf433.title": "433 MHz cihazları",
  "rf433.empty": "Henüz 433 MHz cihazı görülmedi",
  "rf433.refresh": "Yenile",
  "dbstats.title": "Veritabanı",
  "scene.title": "Tüm ev",
  "scene.lightsOff": "Tüm ışıkları kapat",
  "scene.lightsOn": "Tüm ışıkları aç",
  "scene.blindsUp": "Tüm panjurları kaldır",
  "scene.blindsDown": "Tüm panjurları indir",
  "rooms.empty": "Cihaz yok",
  "rooms.other": "Diğer",
  "rooms.editIcons": "Simgeleri düzenle",
  "rooms.editIconsDone": "Bitti",
  "rooms.iconNone": "—",
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
  "state.motion": "hareket",
  "state.noMotion": "hareket yok",

  "ctl.mode": "Mod",
  "ctl.brightness": "Parlaklık",
  "klima.cool": "Soğutma",
  "klima.heat": "Isıtma",
  "klima.auto": "Otomatik",
  "klima.dry": "Nem alma",
  "klima.fan": "Fan",
};

export default messages;
