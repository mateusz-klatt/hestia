import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} 已确认",
  "header.unknown": "{unknown} 未知",

  "conn.reconnecting": "(正在重新连接…)",

  "view.rooms": "🏠 房间",
  "view.back": "← 房间",
  "view.advanced": "🔧 高级",

  "login.username": "用户名",
  "login.password": "密码",
  "login.submit": "登录",
  "login.error": "✗ 用户名或密码错误",

  "user.loggedInAs": "已登录：{user}",
  "user.logout": "退出登录",
  "user.language": "语言",
  "user.temperature": "温度",

  "common.loading": "加载中…",
  "rooms.empty": "没有设备",
  "rooms.other": "其他",
  "rooms.deviceCount.other": "{n} 个设备",

  "ctl.on": "开启",
  "ctl.off": "关闭",
  "ctl.raise": "调高",
  "ctl.lower": "调低",
  "ctl.set": "设置",
  "ctl.turnOff": "关闭",
  "ctl.sent": "✓ 已发送",
  "ctl.failed": "失败",
  "ctl.error": "✗ 错误",

  "state.open": "开",
  "state.closed": "关",
};

export default messages;
