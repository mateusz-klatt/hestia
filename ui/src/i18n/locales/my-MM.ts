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
  "rf433.title": "433 MHz စက်များ",
  "rf433.empty": "433 MHz စက်များ မတွေ့ရသေးပါ",
  "rf433.refresh": "အသစ်တင်",
  "dbstats.title": "ဒေတာဘေ့စ်",
  "scene.title": "တစ်အိမ်လုံး",
  "scene.lightsOff": "မီးအားလုံးပိတ်",
  "scene.lightsOn": "မီးအားလုံးဖွင့်",
  "scene.blindsUp": "ဘလိုင်းအားလုံးတင်",
  "scene.blindsDown": "ဘလိုင်းအားလုံးချ",
  "rooms.empty": "စက်ပစ္စည်းမရှိပါ",
  "rooms.other": "အခြား",
  "rooms.editIcons": "အိုင်ကွန်များ ပြင်ဆင်ရန်",
  "rooms.editIconsDone": "ပြီးပါပြီ",
  "rooms.iconNone": "—",
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
  "state.motion": "လှုပ်ရှားမှု",
  "state.noMotion": "လှုပ်ရှားမှု မရှိပါ",
};

export default messages;
