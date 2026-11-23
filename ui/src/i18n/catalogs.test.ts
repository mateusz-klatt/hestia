import { describe, expect, it } from "vitest";

import { LOCALES, type Locale } from "./index";
import en, { type MessageKey } from "./locales/en";

// Import the files themselves: loadLocale/t would hide missing entries behind English fallback.
const catalogs = import.meta.glob<Record<string, string>>("./locales/*.ts", {
  eager: true,
  import: "default",
});
const sourceKeys = Object.keys(en).sort();
const pluralBases = [...new Set(sourceKeys.flatMap((key) => {
  const match = /^(.*)\.(zero|one|two|few|many)$/.exec(key);
  // A standalone label such as rooms.other is not a plural family.
  return match?.[1] !== undefined && `${match[1]}.other` in en ? [match[1]] : [];
}))];

interface IdenticalString {
  value: string;
  locales: "all" | readonly Locale[];
}

// Exact key + value exceptions, never a blanket exemption for a language or an English word.
// These are brands, symbols, protocol identifiers and literal input-format hints.
const identicalStrings: Partial<Record<MessageKey, IdenticalString>> = {
  "header.title": { value: "hestia", locales: "all" },
  "audit.result.ok": { value: "OK", locales: "all" },
  "rooms.iconNone": { value: "—", locales: "all" },
  "auto.id": { value: "id", locales: "all" },
  "rule.id": { value: "id", locales: "all" },
  "auto.trig.stateGlobal": { value: "{field} {op} {value}", locales: "all" },
  "auto.trig.cron": { value: "cron {expr}", locales: "all" },
  "rule.phWindowStart": { value: "HH:MM", locales: "all" },
  "rule.phWindowEnd": { value: "HH:MM", locales: "all" },

  // Reviewed native cognates and established UI loanwords; scoped to their actual locales.
  "role.admin": { value: "Admin", locales: ["da", "de", "fil", "fr", "hu", "id", "it", "ms", "sv", "tr"] },
  "role.operator": { value: "Operator", locales: ["bs", "de", "fil", "hr", "id", "ms", "nl", "pl", "ro", "sq"] },
  "login.username": { value: "Username", locales: ["fil"] },
  "login.password": { value: "Password", locales: ["fil", "it"] },
  "users.username": { value: "Username", locales: ["fil"] },
  "users.password": { value: "Password", locales: ["fil", "it"] },
  "users.role": { value: "Role", locales: ["cs"] },
  "audit.actor.system": { value: "System", locales: ["da", "de", "no", "pl", "sv"] },
  "audit.actor.automation": { value: "Automation: {id}", locales: ["fil"] },
  "audit.action.switch": { value: "Switch", locales: ["fil"] },
  "audit.action.ir": { value: "IR signal", locales: ["bs", "hr"] },
  "audit.action.scene": { value: "Scene", locales: ["da", "no"] },
  "audit.action.login": { value: "Login", locales: ["da", "pt"] },
  "audit.result.invalid": { value: "Invalid", locales: ["ro"] },
  "dbstats.title": { value: "Database", locales: ["da", "fil", "it", "nl", "no"] },
  "scene.badge": { value: "⏏ scene {id}", locales: ["da", "no"] },
  "ctl.sent": { value: "✓ sent", locales: ["is"] },
  "ctl.error": { value: "✗ error", locales: ["es", "fil"] },
  "ctl.mode": { value: "Mode", locales: ["fil", "fr", "id"] },
  "ctl.position": { value: "Position", locales: ["da", "de", "fr", "sv"] },
  "state.open": { value: "open", locales: ["nl"] },
  "klima.auto": { value: "Auto", locales: ["cs", "da", "de", "es", "fi", "fil", "fr", "hu", "it", "ms", "nl", "no", "pl", "pt", "ro", "sk", "sv"] },
  "klima.fan": { value: "Fan", locales: ["tr"] },
  "tbl.node": { value: "node", locales: ["da", "fil", "no"] },
  "tbl.actions": { value: "actions", locales: ["fr"] },
  "dev.channel": { value: "↳ channel {ep}", locales: ["fil"] },
  "type.blind": { value: "blind", locales: ["fil"] },
  "type.thermostat": { value: "thermostat", locales: ["fil", "fr"] },
  "type.plug": { value: "plug", locales: ["fil"] },
  "type.water": { value: "water", locales: ["nl"] },
  "mode.plain": { value: "mode: {mode}", locales: ["fil", "id"] },
  "auto.trigger": { value: "trigger", locales: ["fil", "it", "nl"] },
  "auto.conditions": { value: "conditions", locales: ["fr"] },
  "auto.actions": { value: "actions", locales: ["fr"] },
  "auto.edit": { value: "Edit", locales: ["id", "ms"] },
  "auto.error": { value: "error {status}", locales: ["es", "fil"] },
  "auto.trig.scene": { value: "scene {id} @node {node}", locales: ["da", "no"] },
  "auto.trig.state": { value: "node {node}{gang} {field} {op} {value}", locales: ["da", "fil", "no"] },
  "rule.error": { value: "error {status}", locales: ["es", "fil"] },
  "rule.trigger": { value: "trigger", locales: ["fil", "it", "nl"] },
  "rule.addCondition": { value: "+ condition", locales: ["fr"] },
  "rule.addAction": { value: "+ action", locales: ["fr"] },
  "rule.rcCondition": { value: "condition #{n}", locales: ["fr"] },
  "rule.phNode": { value: "node", locales: ["da", "fil", "no"] },
  "rule.errGeneric": { value: "error", locales: ["es", "fil"] },

  // PHASE-2: add other locales once budget.* is translated if legitimately identical in those languages.
  "budget.main.scopeWithState": { value: "{scope}: {state}", locales: ["pl", "ja"] },
  "budget.year.noData": { value: "—", locales: ["pl", "ja"] },
  "budget.export.xlsx": { value: "XLSX", locales: ["pl", "ja"] },
  "budget.export.csv": { value: "CSV", locales: ["pl", "ja"] },
  "budget.export.colStatus": { value: "Status", locales: ["pl"] },
  "budget.rules.conditionSummary": { value: "{field} {operator} „{value}”", locales: ["pl", "ja"] },
  "budget.mortgages.windowRange": { value: "{start} – {end}", locales: ["pl", "ja"] },
  "budget.monthBudget.limitColumn": { value: "Limit", locales: ["pl"] },
  "budget.monthBudget.noLimit": { value: "—", locales: ["pl", "ja"] },
  "budget.monthBudget.statusColumn": { value: "Status", locales: ["pl"] },
  "budget.monthBudget.noValue": { value: "—", locales: ["pl", "ja"] },
};

// PHASE-2: budget.* translations pending
// Temporarily tolerate missing budget.* keys in the 43 non-en/non-pl locales until phase 2 fills them.
function isPendingBudgetKey(locale: Locale, key: string, messages: Record<string, string>): boolean {
  return locale !== "en" && locale !== "pl" && key.startsWith("budget.") && !(key in messages);
}

function placeholders(message: string): string[] {
  // Compare sets, so translators may reorder or repeat a parameter. Include malformed/non-ASCII
  // names as well, so a translated token cannot silently escape the runtime's {\w+} interpolation.
  return [...new Set(message.match(/\{[^{}]*\}/g) ?? [])].sort();
}

describe("translation catalogs", () => {
  it("ships exactly one catalog for every supported locale", () => {
    expect(Object.keys(catalogs).sort()).toEqual(LOCALES.map((locale) => `./locales/${locale}.ts`).sort());
  });

  it("keeps identical-string exceptions explicit and current", () => {
    for (const [key, exception] of Object.entries(identicalStrings)) {
      expect(en[key as MessageKey], key).toBe(exception.value);
      const locales = exception.locales === "all" ? LOCALES.filter((locale) => locale !== "en") : exception.locales;
      expect(locales.length, key).toBeGreaterThan(0);
      for (const locale of locales) {
        expect(catalogs[`./locales/${locale}.ts`]?.[key], `${locale}: ${key}`).toBe(exception.value);
      }
    }
  });

  describe.each(LOCALES)("%s", (locale) => {
    const messages = catalogs[`./locales/${locale}.ts`] ?? {};

    it("has every source key and no extra keys", () => {
      const expectedKeys = sourceKeys.filter((key) => !isPendingBudgetKey(locale, key, messages));
      expect(Object.keys(messages).sort()).toEqual(expectedKeys);
    });

    it("has non-empty messages with the source placeholder sets", () => {
      for (const [key, source] of Object.entries(en)) {
        if (isPendingBudgetKey(locale, key, messages)) continue;
        const message = messages[key];
        expect(typeof message, key).toBe("string");
        expect(message?.trim(), key).not.toBe("");
        expect(placeholders(message ?? ""), key).toEqual(placeholders(source));
      }
    });

    it("provides each cardinal plural category required by Intl.PluralRules", () => {
      const categories = new Intl.PluralRules(locale).resolvedOptions().pluralCategories;
      for (const base of pluralBases) {
        for (const category of categories) {
          const key = `${base}.${category}`;
          if (isPendingBudgetKey(locale, key, messages)) continue;
          expect(messages[key]?.trim(), key).toBeTruthy();
        }
      }
    });

    it("only shares explicitly reviewed strings with English", () => {
      if (locale === "en") return;
      const unreviewed = Object.entries(en).flatMap(([key, source]) => {
        if (messages[key] !== source) return [];
        const exception = identicalStrings[key as MessageKey];
        const allowed = exception?.value === source
          && (exception.locales === "all" || exception.locales.includes(locale));
        return allowed ? [] : [key];
      });
      expect(unreviewed).toEqual([]);
    });
  });
});
