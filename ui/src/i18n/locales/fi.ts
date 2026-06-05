import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} vahvistettu",
  "header.unknown": "{unknown} tuntematonta",

  "conn.reconnecting": "(yhdistetään uudelleen…)",

  "view.rooms": "🏠 Huoneet",
  "view.back": "← Huoneet",
  "view.advanced": "🔧 Lisäasetukset",

  "login.username": "Käyttäjätunnus",
  "login.password": "Salasana",
  "login.submit": "Kirjaudu",
  "login.error": "✗ Väärä käyttäjätunnus tai salasana",

  "user.loggedInAs": "kirjautunut: {user}",
  "user.logout": "Kirjaudu ulos",
  "user.language": "Kieli",
  "user.temperature": "Lämpötila",

  "common.loading": "Ladataan…",
  "audit.title": "Toimintaloki",
  "audit.empty": "Ei vielä toimintaa",
  "audit.refresh": "Päivitä",
  "rf433.title": "433 MHz -laitteet",
  "rf433.empty": "433 MHz -laitteita ei ole vielä havaittu",
  "rf433.refresh": "Päivitä",
  "dbstats.title": "Tietokanta",
  "scene.title": "Koko koti",
  "scene.lightsOff": "Sammuta kaikki valot",
  "scene.lightsOn": "Sytytä kaikki valot",
  "scene.blindsUp": "Nosta kaikki kaihtimet",
  "scene.blindsDown": "Laske kaikki kaihtimet",
  "rooms.empty": "Ei laitteita",
  "rooms.other": "Muut",
  "rooms.editIcons": "Muokkaa kuvakkeita",
  "rooms.editIconsDone": "Valmis",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} laite",
  "rooms.deviceCount.other": "{n} laitetta",

  "ctl.on": "Päällä",
  "ctl.off": "Pois",
  "ctl.raise": "Nosta",
  "ctl.lower": "Laske",
  "ctl.set": "Aseta",
  "ctl.turnOff": "Sammuta",
  "ctl.sent": "✓ lähetetty",
  "ctl.failed": "epäonnistui",
  "ctl.error": "✗ virhe",

  "state.open": "auki",
  "state.closed": "kiinni",
  "state.motion": "liike",
  "state.noMotion": "ei liikettä",

  "ctl.mode": "Tila",
  "ctl.brightness": "Kirkkaus",
  "klima.cool": "Jäähdytys",
  "klima.heat": "Lämmitys",
  "klima.auto": "Auto",
  "klima.dry": "Kuivaus",
  "klima.fan": "Puhallin",
};

export default messages;
