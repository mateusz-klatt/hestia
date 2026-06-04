import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} ยืนยันแล้ว",
  "header.unknown": "{unknown} ไม่ทราบ",

  "conn.reconnecting": "(กำลังเชื่อมต่อใหม่…)",

  "view.rooms": "🏠 ห้อง",
  "view.back": "← ห้อง",
  "view.advanced": "🔧 ขั้นสูง",

  "login.username": "ชื่อผู้ใช้",
  "login.password": "รหัสผ่าน",
  "login.submit": "ลงชื่อเข้าใช้",
  "login.error": "✗ ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง",

  "user.loggedInAs": "ลงชื่อเข้าใช้แล้ว: {user}",
  "user.logout": "ออกจากระบบ",
  "user.language": "ภาษา",
  "user.temperature": "อุณหภูมิ",

  "common.loading": "กำลังโหลด…",
  "audit.title": "บันทึกกิจกรรม",
  "audit.empty": "ยังไม่มีกิจกรรม",
  "audit.refresh": "รีเฟรช",
  "rf433.title": "อุปกรณ์ 433 MHz",
  "rf433.empty": "ยังไม่พบอุปกรณ์ 433 MHz",
  "rf433.refresh": "รีเฟรช",
  "dbstats.title": "ฐานข้อมูล",
  "scene.title": "ทั้งบ้าน",
  "scene.lightsOff": "ปิดไฟทั้งหมด",
  "scene.lightsOn": "เปิดไฟทั้งหมด",
  "scene.blindsUp": "ยกมู่ลี่ทั้งหมด",
  "scene.blindsDown": "ลดมู่ลี่ทั้งหมด",
  "rooms.empty": "ไม่มีอุปกรณ์",
  "rooms.other": "อื่นๆ",
  "rooms.editIcons": "แก้ไขไอคอน",
  "rooms.editIconsDone": "เสร็จสิ้น",
  "rooms.iconNone": "—",
  "rooms.deviceCount.other": "{n} อุปกรณ์",

  "ctl.on": "เปิด",
  "ctl.off": "ปิด",
  "ctl.raise": "เพิ่ม",
  "ctl.lower": "ลด",
  "ctl.set": "ตั้งค่า",
  "ctl.turnOff": "ปิด",
  "ctl.sent": "✓ ส่งแล้ว",
  "ctl.failed": "ล้มเหลว",
  "ctl.error": "✗ ข้อผิดพลาด",

  "state.open": "เปิด",
  "state.closed": "ปิด",
  "state.motion": "การเคลื่อนไหว",
  "state.noMotion": "ไม่มีการเคลื่อนไหว",
};

export default messages;
