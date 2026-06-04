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
  "rf433.title": "433 MHz ডিভাইস",
  "rf433.empty": "এখনও কোনো 433 MHz ডিভাইস দেখা যায়নি",
  "rf433.refresh": "রিফ্রেশ করুন",
  "dbstats.title": "ডাটাবেস",
  "scene.title": "পুরো বাড়ি",
  "scene.lightsOff": "সব আলো বন্ধ",
  "scene.lightsOn": "সব আলো চালু",
  "scene.blindsUp": "সব ব্লাইন্ড তুলুন",
  "scene.blindsDown": "সব ব্লাইন্ড নামান",
  "rooms.empty": "কোনো ডিভাইস নেই",
  "rooms.other": "অন্যান্য",
  "rooms.editIcons": "আইকন সম্পাদনা",
  "rooms.editIconsDone": "সম্পন্ন",
  "rooms.iconNone": "—",
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
