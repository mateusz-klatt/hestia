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
  "rooms.empty": "Ei laitteita",
  "rooms.other": "Muut",
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
};

export default messages;
