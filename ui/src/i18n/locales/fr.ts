import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} confirmés",
  "header.unknown": "{unknown} inconnus",

  "conn.reconnecting": "(reconnexion…)",

  "view.rooms": "🏠 Pièces",
  "view.back": "← Pièces",
  "view.advanced": "🔧 Avancé",

  "login.username": "Nom d'utilisateur",
  "login.password": "Mot de passe",
  "login.submit": "Se connecter",
  "login.error": "✗ Nom d'utilisateur ou mot de passe incorrect",

  "user.loggedInAs": "connecté : {user}",
  "user.logout": "Se déconnecter",
  "user.language": "Langue",
  "user.temperature": "Température",

  "common.loading": "Chargement…",
  "audit.title": "Journal d’activité",
  "audit.empty": "Aucune activité pour l’instant",
  "audit.refresh": "Actualiser",
  "dbstats.title": "Base de données",
  "scene.title": "Toute la maison",
  "scene.lightsOff": "Éteindre toutes les lumières",
  "scene.lightsOn": "Allumer toutes les lumières",
  "scene.blindsUp": "Monter tous les stores",
  "scene.blindsDown": "Baisser tous les stores",
  "rooms.empty": "Aucun appareil",
  "rooms.other": "Autres",
  "rooms.editIcons": "Modifier les icônes",
  "rooms.iconNone": "—",
  "rooms.deviceCount.one": "{n} appareil",
  "rooms.deviceCount.many": "{n} appareils",
  "rooms.deviceCount.other": "{n} appareils",

  "ctl.on": "Activé",
  "ctl.off": "Désactivé",
  "ctl.raise": "Augmenter",
  "ctl.lower": "Réduire",
  "ctl.set": "Régler",
  "ctl.turnOff": "Éteindre",
  "ctl.sent": "✓ envoyé",
  "ctl.failed": "échec",
  "ctl.error": "✗ erreur",

  "state.open": "ouvert",
  "state.closed": "fermé",
};

export default messages;
