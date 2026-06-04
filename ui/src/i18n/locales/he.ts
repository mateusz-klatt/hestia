import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} אושרו",
  "header.unknown": "{unknown} לא ידועים",

  "conn.reconnecting": "(מתחבר מחדש…)",

  "view.rooms": "🏠 חדרים",
  "view.back": "← חדרים",
  "view.advanced": "🔧 מתקדם",

  "login.username": "שם משתמש",
  "login.password": "סיסמה",
  "login.submit": "כניסה",
  "login.error": "✗ שם המשתמש או הסיסמה שגויים",

  "user.loggedInAs": "מחובר: {user}",
  "user.logout": "יציאה",
  "user.language": "שפה",
  "user.temperature": "טמפרטורה",

  "common.loading": "טוען…",
  "audit.title": "יומן פעילות",
  "audit.empty": "אין פעילות עדיין",
  "audit.refresh": "רענן",
  "rf433.title": "התקני 433 MHz",
  "rf433.empty": "עדיין לא זוהו התקני 433 MHz",
  "rf433.refresh": "רענן",
  "dbstats.title": "מסד נתונים",
  "scene.title": "כל הבית",
  "scene.lightsOff": "כבה את כל האורות",
  "scene.lightsOn": "הדלק את כל האורות",
  "scene.blindsUp": "הרם את כל התריסים",
  "scene.blindsDown": "הורד את כל התריסים",
  "rooms.empty": "אין מכשירים",
  "rooms.other": "אחרים",
  "rooms.editIcons": "עריכת סמלים",
  "rooms.editIconsDone": "סיום",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} מכשיר",
  "rooms.deviceCount.two": "{n} מכשירים",
  "rooms.deviceCount.other": "{n} מכשירים",

  "ctl.on": "פועל",
  "ctl.off": "כבוי",
  "ctl.raise": "להעלות",
  "ctl.lower": "להוריד",
  "ctl.set": "הגדר",
  "ctl.turnOff": "כבה",
  "ctl.sent": "✓ נשלח",
  "ctl.failed": "נכשל",
  "ctl.error": "✗ שגיאה",

  "state.open": "פתוח",
  "state.closed": "סגור",
  "state.motion": "תנועה",
  "state.noMotion": "אין תנועה",
};

export default messages;
