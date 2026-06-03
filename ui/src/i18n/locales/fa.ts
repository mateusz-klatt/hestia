import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} تأیید شده",
  "header.unknown": "{unknown} ناشناس",

  "conn.reconnecting": "(در حال اتصال مجدد…)",

  "view.rooms": "🏠 اتاق‌ها",
  "view.back": "← اتاق‌ها",
  "view.advanced": "🔧 پیشرفته",

  "login.username": "نام کاربری",
  "login.password": "گذرواژه",
  "login.submit": "ورود",
  "login.error": "✗ نام کاربری یا گذرواژه نادرست است",

  "user.loggedInAs": "وارد شده: {user}",
  "user.logout": "خروج",
  "user.language": "زبان",
  "user.temperature": "دما",

  "common.loading": "در حال بارگذاری…",
  "audit.title": "گزارش فعالیت",
  "audit.empty": "هنوز فعالیتی نیست",
  "audit.refresh": "بازخوانی",
  "dbstats.title": "پایگاه داده",
  "rooms.empty": "هیچ دستگاهی نیست",
  "rooms.other": "سایر",
  "rooms.deviceCount.one": "{n} دستگاه",
  "rooms.deviceCount.other": "{n} دستگاه",

  "ctl.on": "روشن",
  "ctl.off": "خاموش",
  "ctl.raise": "افزایش",
  "ctl.lower": "کاهش",
  "ctl.set": "تنظیم",
  "ctl.turnOff": "خاموش کردن",
  "ctl.sent": "✓ ارسال شد",
  "ctl.failed": "ناموفق",
  "ctl.error": "✗ خطا",

  "state.open": "باز",
  "state.closed": "بسته",
};

export default messages;
