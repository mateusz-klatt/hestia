import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} đã xác nhận",
  "header.unknown": "{unknown} không xác định",

  "conn.reconnecting": "(đang kết nối lại…)",

  "view.rooms": "🏠 Phòng",
  "view.back": "← Phòng",
  "view.advanced": "🔧 Nâng cao",

  "login.username": "Tên người dùng",
  "login.password": "Mật khẩu",
  "login.submit": "Đăng nhập",
  "login.error": "✗ Sai tên người dùng hoặc mật khẩu",

  "user.loggedInAs": "đã đăng nhập: {user}",
  "user.logout": "Đăng xuất",
  "user.language": "Ngôn ngữ",
  "user.temperature": "Nhiệt độ",

  "common.loading": "Đang tải…",
  "rooms.empty": "Không có thiết bị",
  "rooms.other": "Khác",
  "rooms.deviceCount.other": "{n} thiết bị",

  "ctl.on": "Bật",
  "ctl.off": "Tắt",
  "ctl.raise": "Tăng",
  "ctl.lower": "Giảm",
  "ctl.set": "Đặt",
  "ctl.turnOff": "Tắt",
  "ctl.sent": "✓ đã gửi",
  "ctl.failed": "thất bại",
  "ctl.error": "✗ lỗi",

  "state.open": "mở",
  "state.closed": "đóng",
};

export default messages;
