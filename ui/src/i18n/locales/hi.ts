import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} पुष्टि की गई",
  "header.unknown": "{unknown} अज्ञात",

  "conn.reconnecting": "(फिर से कनेक्ट हो रहा है…)",

  "view.rooms": "🏠 कमरे",
  "view.back": "← कमरे",
  "view.advanced": "🔧 उन्नत",

  "login.username": "उपयोगकर्ता नाम",
  "login.password": "पासवर्ड",
  "login.submit": "साइन इन",
  "login.error": "✗ उपयोगकर्ता नाम या पासवर्ड गलत है",

  "user.loggedInAs": "साइन इन: {user}",
  "user.logout": "लॉग आउट",
  "user.language": "भाषा",
  "user.temperature": "तापमान",

  "common.loading": "लोड हो रहा है…",
  "audit.title": "गतिविधि लॉग",
  "audit.empty": "अभी कोई गतिविधि नहीं",
  "audit.refresh": "रीफ़्रेश करें",
  "dbstats.title": "डेटाबेस",
  "rooms.empty": "कोई डिवाइस नहीं",
  "rooms.other": "अन्य",
  "rooms.deviceCount.one": "{n} डिवाइस",
  "rooms.deviceCount.other": "{n} डिवाइस",

  "ctl.on": "चालू",
  "ctl.off": "बंद",
  "ctl.raise": "बढ़ाएँ",
  "ctl.lower": "घटाएँ",
  "ctl.set": "सेट करें",
  "ctl.turnOff": "बंद करें",
  "ctl.sent": "✓ भेजा गया",
  "ctl.failed": "विफल",
  "ctl.error": "✗ त्रुटि",

  "state.open": "खुला",
  "state.closed": "बंद",
};

export default messages;
