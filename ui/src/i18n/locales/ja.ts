import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} 確認済み",
  "header.unknown": "{unknown} 不明",

  "conn.reconnecting": "(再接続中…)",

  "view.rooms": "🏠 部屋",
  "view.back": "← 部屋",
  "view.advanced": "🔧 詳細",

  "login.username": "ユーザー名",
  "login.password": "パスワード",
  "login.submit": "サインイン",
  "login.error": "✗ ユーザー名またはパスワードが正しくありません",

  "user.loggedInAs": "サインイン中: {user}",
  "user.logout": "ログアウト",
  "user.language": "言語",
  "user.temperature": "温度",

  "common.loading": "読み込み中…",
  "audit.title": "アクティビティログ",
  "audit.empty": "まだアクティビティはありません",
  "audit.refresh": "更新",
  "rf433.title": "433 MHz デバイス",
  "rf433.empty": "433 MHz デバイスはまだ検出されていません",
  "rf433.refresh": "更新",
  "dbstats.title": "データベース",
  "scene.title": "家全体",
  "scene.lightsOff": "すべての照明をオフ",
  "scene.lightsOn": "すべての照明をオン",
  "scene.blindsUp": "すべてのブラインドを上げる",
  "scene.blindsDown": "すべてのブラインドを下げる",
  "rooms.empty": "デバイスがありません",
  "rooms.other": "その他",
  "rooms.editIcons": "アイコンを編集",
  "rooms.editIconsDone": "完了",
  "rooms.iconNone": "—",
  "rooms.deviceCount.other": "{n} 台のデバイス",

  "ctl.on": "オン",
  "ctl.off": "オフ",
  "ctl.raise": "上げる",
  "ctl.lower": "下げる",
  "ctl.set": "設定",
  "ctl.turnOff": "オフにする",
  "ctl.sent": "✓ 送信済み",
  "ctl.failed": "失敗",
  "ctl.error": "✗ エラー",

  "state.open": "開いている",
  "state.closed": "閉じている",
  "state.motion": "動き",
  "state.noMotion": "動きなし",
};

export default messages;
