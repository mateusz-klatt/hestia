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
  "rf433.title": "دستگاه‌های 433 MHz",
  "rf433.empty": "هنوز هیچ دستگاه 433 MHz مشاهده نشده است",
  "rf433.refresh": "بازخوانی",
  "dbstats.title": "پایگاه داده",
  "scene.title": "همه خانه",
  "scene.lightsOff": "خاموش کردن همه چراغ‌ها",
  "scene.lightsOn": "روشن کردن همه چراغ‌ها",
  "scene.blindsUp": "بالا بردن همه پرده‌ها",
  "scene.blindsDown": "پایین آوردن همه پرده‌ها",
  "rooms.empty": "هیچ دستگاهی نیست",
  "rooms.other": "سایر",
  "rooms.editIcons": "ویرایش نمادها",
  "rooms.editIconsDone": "تمام",
  "rooms.iconNone": "—",
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
