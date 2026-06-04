import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} 已確認",
  "header.unknown": "{unknown} 未知",

  "conn.reconnecting": "(正在重新連線…)",

  "view.rooms": "🏠 房間",
  "view.back": "← 房間",
  "view.advanced": "🔧 進階",

  "login.username": "使用者名稱",
  "login.password": "密碼",
  "login.submit": "登入",
  "login.error": "✗ 使用者名稱或密碼錯誤",

  "user.loggedInAs": "已登入：{user}",
  "user.logout": "登出",
  "user.language": "語言",
  "user.temperature": "溫度",

  "common.loading": "載入中…",
  "audit.title": "活動記錄",
  "audit.empty": "尚無活動",
  "audit.refresh": "重新整理",
  "rf433.title": "433 MHz 裝置",
  "rf433.empty": "尚未偵測到 433 MHz 裝置",
  "rf433.refresh": "重新整理",
  "dbstats.title": "資料庫",
  "scene.title": "全屋",
  "scene.lightsOff": "關閉所有燈光",
  "scene.lightsOn": "開啟所有燈光",
  "scene.blindsUp": "升起所有百葉窗",
  "scene.blindsDown": "降下所有百葉窗",
  "rooms.empty": "沒有裝置",
  "rooms.other": "其他",
  "rooms.editIcons": "編輯圖示",
  "rooms.editIconsDone": "完成",
  "rooms.iconNone": "—",
  "rooms.deviceCount.other": "{n} 個裝置",

  "ctl.on": "開啟",
  "ctl.off": "關閉",
  "ctl.raise": "調高",
  "ctl.lower": "調低",
  "ctl.set": "設定",
  "ctl.turnOff": "關閉",
  "ctl.sent": "✓ 已傳送",
  "ctl.failed": "失敗",
  "ctl.error": "✗ 錯誤",

  "state.open": "開",
  "state.closed": "關",
  "state.motion": "有動作",
  "state.noMotion": "無動作",
};

export default messages;
