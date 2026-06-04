import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} հաստատված",
  "header.unknown": "{unknown} անհայտ",

  "conn.reconnecting": "(վերամիացում…)",

  "view.rooms": "🏠 Սենյակներ",
  "view.back": "← Սենյակներ",
  "view.advanced": "🔧 Ընդլայնված",

  "login.username": "Օգտանուն",
  "login.password": "Գաղտնաբառ",
  "login.submit": "Մուտք",
  "login.error": "✗ Սխալ օգտանուն կամ գաղտնաբառ",

  "user.loggedInAs": "մուտք գործած: {user}",
  "user.logout": "Ելք",
  "user.language": "Լեզու",
  "user.temperature": "Ջերմաստիճան",

  "common.loading": "Բեռնվում է…",
  "audit.title": "Գործունեության մատյան",
  "audit.empty": "Դեռ գործունեություն չկա",
  "audit.refresh": "Թարմացնել",
  "rf433.title": "433 MHz սարքեր",
  "rf433.empty": "433 MHz սարքեր դեռ չեն հայտնաբերվել",
  "rf433.refresh": "Թարմացնել",
  "dbstats.title": "Տվյալների բազա",
  "scene.title": "Ամբողջ տունը",
  "scene.lightsOff": "Անջատել բոլոր լույսերը",
  "scene.lightsOn": "Միացնել բոլոր լույսերը",
  "scene.blindsUp": "Բարձրացնել բոլոր շերտավարագույրները",
  "scene.blindsDown": "Իջեցնել բոլոր շերտավարագույրները",
  "rooms.empty": "Սարքեր չկան",
  "rooms.other": "Այլ",
  "rooms.editIcons": "Խմբագրել պատկերակները",
  "rooms.editIconsDone": "Պատրաստ է",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} սարք",
  "rooms.deviceCount.other": "{n} սարքեր",

  "ctl.on": "Միացված",
  "ctl.off": "Անջատված",
  "ctl.raise": "Բարձրացնել",
  "ctl.lower": "Իջեցնել",
  "ctl.set": "Սահմանել",
  "ctl.turnOff": "Անջատել",
  "ctl.sent": "✓ ուղարկվեց",
  "ctl.failed": "չհաջողվեց",
  "ctl.error": "✗ սխալ",

  "state.open": "բաց",
  "state.closed": "փակ",
  "state.motion": "շարժում",
  "state.noMotion": "շարժում չկա",
};

export default messages;
