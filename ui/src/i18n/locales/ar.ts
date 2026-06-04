import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} مؤكدة",
  "header.unknown": "{unknown} غير معروفة",

  "conn.reconnecting": "(إعادة الاتصال…)",

  "view.rooms": "🏠 الغرف",
  "view.back": "← الغرف",
  "view.advanced": "🔧 متقدم",

  "login.username": "اسم المستخدم",
  "login.password": "كلمة المرور",
  "login.submit": "تسجيل الدخول",
  "login.error": "✗ اسم المستخدم أو كلمة المرور غير صحيحة",

  "user.loggedInAs": "تم تسجيل الدخول: {user}",
  "user.logout": "تسجيل الخروج",
  "user.language": "اللغة",
  "user.temperature": "درجة الحرارة",

  "common.loading": "جارٍ التحميل…",
  "audit.title": "سجل النشاط",
  "audit.empty": "لا يوجد نشاط بعد",
  "audit.refresh": "تحديث",
  "dbstats.title": "قاعدة البيانات",
  "scene.title": "المنزل كله",
  "scene.lightsOff": "إطفاء كل الأضواء",
  "scene.lightsOn": "تشغيل كل الأضواء",
  "scene.blindsUp": "رفع كل الستائر",
  "scene.blindsDown": "إنزال كل الستائر",
  "rooms.empty": "لا توجد أجهزة",
  "rooms.other": "أخرى",
  "rooms.editIcons": "تعديل الأيقونات",
  "rooms.editIconsDone": "تم",
  "rooms.iconNone": "—",
  "rooms.deviceCount.zero": "{n} جهاز",
  "rooms.deviceCount.one": "{n} جهاز",
  "rooms.deviceCount.two": "{n} جهازان",
  "rooms.deviceCount.few": "{n} أجهزة",
  "rooms.deviceCount.many": "{n} جهازًا",
  "rooms.deviceCount.other": "{n} جهاز",

  "ctl.on": "تشغيل",
  "ctl.off": "إيقاف",
  "ctl.raise": "رفع",
  "ctl.lower": "خفض",
  "ctl.set": "تعيين",
  "ctl.turnOff": "إيقاف التشغيل",
  "ctl.sent": "✓ تم الإرسال",
  "ctl.failed": "فشل",
  "ctl.error": "✗ خطأ",

  "state.open": "مفتوح",
  "state.closed": "مغلق",
};

export default messages;
