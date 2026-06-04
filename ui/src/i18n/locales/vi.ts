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
  "audit.title": "Nhật ký hoạt động",
  "audit.empty": "Chưa có hoạt động",
  "audit.refresh": "Làm mới",
  "rf433.title": "Thiết bị 433 MHz",
  "rf433.empty": "Chưa thấy thiết bị 433 MHz nào",
  "rf433.refresh": "Làm mới",
  "dbstats.title": "Cơ sở dữ liệu",
  "scene.title": "Toàn nhà",
  "scene.lightsOff": "Tắt tất cả đèn",
  "scene.lightsOn": "Bật tất cả đèn",
  "scene.blindsUp": "Nâng tất cả rèm",
  "scene.blindsDown": "Hạ tất cả rèm",
  "rooms.empty": "Không có thiết bị",
  "rooms.other": "Khác",
  "rooms.editIcons": "Chỉnh sửa biểu tượng",
  "rooms.editIconsDone": "Xong",
  "rooms.iconNone": "—",
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
