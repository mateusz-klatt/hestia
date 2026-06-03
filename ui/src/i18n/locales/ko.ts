import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} 확인됨",
  "header.unknown": "{unknown} 알 수 없음",

  "conn.reconnecting": "(다시 연결 중…)",

  "view.rooms": "🏠 방",
  "view.back": "← 방",
  "view.advanced": "🔧 고급",

  "login.username": "사용자 이름",
  "login.password": "비밀번호",
  "login.submit": "로그인",
  "login.error": "✗ 사용자 이름 또는 비밀번호가 잘못되었습니다",

  "user.loggedInAs": "로그인됨: {user}",
  "user.logout": "로그아웃",
  "user.language": "언어",
  "user.temperature": "온도",

  "common.loading": "로딩 중…",
  "audit.title": "활동 기록",
  "audit.empty": "아직 활동이 없습니다",
  "audit.refresh": "새로고침",
  "dbstats.title": "데이터베이스",
  "scene.title": "집 전체",
  "scene.lightsOff": "모든 조명 끄기",
  "scene.lightsOn": "모든 조명 켜기",
  "scene.blindsUp": "모든 블라인드 올리기",
  "scene.blindsDown": "모든 블라인드 내리기",
  "rooms.empty": "기기가 없습니다",
  "rooms.other": "기타",
  "rooms.editIcons": "아이콘 편집",
  "rooms.iconNone": "—",
  "rooms.deviceCount.other": "{n}개 기기",

  "ctl.on": "켜짐",
  "ctl.off": "꺼짐",
  "ctl.raise": "올리기",
  "ctl.lower": "내리기",
  "ctl.set": "설정",
  "ctl.turnOff": "끄기",
  "ctl.sent": "✓ 전송됨",
  "ctl.failed": "실패",
  "ctl.error": "✗ 오류",

  "state.open": "열림",
  "state.closed": "닫힘",
};

export default messages;
