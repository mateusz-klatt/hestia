import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} নিশ্চিত",
  "header.unknown": "{unknown} অজানা",

  "conn.reconnecting": "(পুনরায় সংযোগ করা হচ্ছে…)",

  "view.rooms": "🏠 ঘর",
  "view.back": "← ঘর",
  "view.advanced": "🔧 উন্নত",

  "login.username": "ব্যবহারকারীর নাম",
  "login.password": "পাসওয়ার্ড",
  "login.submit": "সাইন ইন",
  "login.error": "✗ ব্যবহারকারীর নাম বা পাসওয়ার্ড ভুল",

  "user.loggedInAs": "লগইন করেছেন: {user}",
  "user.logout": "লগ আউট",
  "user.language": "ভাষা",
  "user.temperature": "তাপমাত্রা",

  "common.loading": "লোড হচ্ছে…",
  "audit.title": "কার্যকলাপ লগ",
  "audit.empty": "এখনও কোনো কার্যকলাপ নেই",
  "audit.refresh": "রিফ্রেশ করুন",
  "rooms.empty": "কোনো ডিভাইস নেই",
  "rooms.other": "অন্যান্য",
  "rooms.deviceCount.one": "{n}টি ডিভাইস",
  "rooms.deviceCount.other": "{n}টি ডিভাইস",

  "ctl.on": "চালু",
  "ctl.off": "বন্ধ",
  "ctl.raise": "বাড়ান",
  "ctl.lower": "কমান",
  "ctl.set": "সেট করুন",
  "ctl.turnOff": "বন্ধ করুন",
  "ctl.sent": "✓ পাঠানো হয়েছে",
  "ctl.failed": "ব্যর্থ",
  "ctl.error": "✗ ত্রুটি",

  "state.open": "খোলা",
  "state.closed": "বন্ধ",
};

export default messages;
