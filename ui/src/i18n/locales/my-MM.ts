import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} အတည်ပြုပြီး",
  "header.unknown": "{unknown} မသိ",

  "conn.reconnecting": "(ပြန်လည်ချိတ်ဆက်နေသည်…)",

  "view.rooms": "🏠 အခန်းများ",
  "view.back": "← အခန်းများ",
  "view.advanced": "🔧 အဆင့်မြင့်",

  "login.username": "အသုံးပြုသူအမည်",
  "login.password": "စကားဝှက်",
  "login.submit": "ဝင်ရန်",
  "login.error": "✗ အသုံးပြုသူအမည် သို့မဟုတ် စကားဝှက် မှားနေသည်",

  "user.loggedInAs": "ဝင်ထားသည်: {user}",
  "user.logout": "ထွက်ရန်",
  "user.language": "ဘာသာစကား",
  "user.temperature": "အပူချိန်",

  "common.loading": "တင်နေသည်…",
  "audit.title": "လုပ်ဆောင်ချက်မှတ်တမ်း",
  "audit.empty": "လုပ်ဆောင်ချက် မရှိသေးပါ",
  "audit.refresh": "အသစ်တင်",
  "rooms.empty": "စက်ပစ္စည်းမရှိပါ",
  "rooms.other": "အခြား",
  "rooms.deviceCount.other": "{n} စက်ပစ္စည်း",

  "ctl.on": "ဖွင့်",
  "ctl.off": "ပိတ်",
  "ctl.raise": "မြှင့်",
  "ctl.lower": "လျှော့",
  "ctl.set": "သတ်မှတ်",
  "ctl.turnOff": "ပိတ်",
  "ctl.sent": "✓ ပို့ပြီး",
  "ctl.failed": "မအောင်မြင်ပါ",
  "ctl.error": "✗ အမှား",

  "state.open": "ဖွင့်ထားသည်",
  "state.closed": "ပိတ်ထားသည်",
};

export default messages;
