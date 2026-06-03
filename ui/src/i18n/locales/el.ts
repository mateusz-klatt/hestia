import type { Messages } from "./en";

const messages: Partial<Messages> = {
  "header.confirmed": "{confirmed}/{total} επιβεβαιωμένα",
  "header.unknown": "{unknown} άγνωστα",

  "conn.reconnecting": "(επανασύνδεση…)",

  "view.rooms": "🏠 Δωμάτια",
  "view.back": "← Δωμάτια",
  "view.advanced": "🔧 Σύνθετα",

  "login.username": "Όνομα χρήστη",
  "login.password": "Κωδικός πρόσβασης",
  "login.submit": "Σύνδεση",
  "login.error": "✗ Λάθος όνομα χρήστη ή κωδικός πρόσβασης",

  "user.loggedInAs": "συνδεδεμένος: {user}",
  "user.logout": "Αποσύνδεση",
  "user.language": "Γλώσσα",
  "user.temperature": "Θερμοκρασία",

  "common.loading": "Φόρτωση…",
  "audit.title": "Αρχείο δραστηριότητας",
  "audit.empty": "Δεν υπάρχει ακόμη δραστηριότητα",
  "audit.refresh": "Ανανέωση",
  "dbstats.title": "Βάση δεδομένων",
  "scene.title": "Όλο το σπίτι",
  "scene.lightsOff": "Σβήσιμο όλων των φώτων",
  "scene.lightsOn": "Άναμμα όλων των φώτων",
  "scene.blindsUp": "Ανέβασμα όλων των περσίδων",
  "scene.blindsDown": "Κατέβασμα όλων των περσίδων",
  "rooms.empty": "Δεν υπάρχουν συσκευές",
  "rooms.other": "Άλλα",
  "rooms.deviceCount.one": "{n} συσκευή",
  "rooms.deviceCount.other": "{n} συσκευές",

  "ctl.on": "Ενεργό",
  "ctl.off": "Ανενεργό",
  "ctl.raise": "Αύξηση",
  "ctl.lower": "Μείωση",
  "ctl.set": "Ρύθμιση",
  "ctl.turnOff": "Απενεργοποίηση",
  "ctl.sent": "✓ στάλθηκε",
  "ctl.failed": "απέτυχε",
  "ctl.error": "✗ σφάλμα",

  "state.open": "ανοιχτό",
  "state.closed": "κλειστό",
};

export default messages;
