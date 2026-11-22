import { useEffect, useRef, useState } from "react";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { accountName, labelOf, pluralPl } from "./model";
import type { AnalyticsQuarantine } from "./model";

export const quarantineLabels = {
  en: {
    title: "Quarantine",
    intro: "Why some bank entries are not counted in period totals. This read-only view contains no amounts or transaction descriptions.",
    summary: "Entries kept out of totals",
    total: "Total",
    accounts: "Counts by account",
    account: "Account",
    accountFilter: "Filter quarantine by account",
    allAccounts: "All accounts",
    excluded: "excluded",
    loading: "Loading quarantine…",
    error: "Quarantine could not be loaded. Try again.",
    empty: "All entries are included in totals.",
    noDate: "No date",
    noStatus: "No status",
    entries: (count: number) => `${String(count)} ${count === 1 ? "entry" : "entries"}`,
    details: (count: number) => `Entry details (${String(count)})`,
    truncated: (shown: number, total: number) =>
      `Showing the newest ${String(shown)} of ${String(total)} ${total === 1 ? "entry" : "entries"}.`,
    buckets: {
      uncertain: "Uncertain entries",
      unresolved: "Unresolved API entries",
      import_unresolved: "Unresolved import entries",
    },
    statuses: {
      BOOK: "Booked",
      PDNG: "Pending",
      HOLD: "On hold",
      CNCL: "Cancelled",
      RJCT: "Rejected",
      SCHD: "Scheduled",
      OTHR: "Other",
    },
    reasons: {
      non_booked_status: "The bank has not booked this entry, so it is excluded from period totals.",
      missing_reference: "The entry has no bank reference, so it cannot be matched safely.",
      missing_date: "The entry has no booking or value date, so it cannot be assigned to a period.",
      reference_conflict: "The same bank reference points to conflicting entry data, so the entry is kept separate.",
      non_canonical: "The offline import contains data in an unsupported format, so the entry was not applied.",
      api_preserved: "The bank API already provided this entry, so an offline import did not replace it.",
      stale_conflict: "The offline import conflicted with newer stored data, so it was not applied.",
      same_time_conflict: "Conflicting versions have the same timestamp, so the correct entry could not be chosen safely.",
    },
    unknownReason: (code: string) => `This entry is excluded for an unrecognized reason (${code}).`,
  },
  pl: {
    title: "Kwarantanna",
    intro: "Wyjaśnienie, dlaczego niektóre wpisy bankowe nie są uwzględnione w sumach okresu. Ten podgląd tylko do odczytu nie zawiera kwot ani opisów transakcji.",
    summary: "Wpisy wyłączone z sum",
    total: "Razem",
    accounts: "Liczby według kont",
    account: "Konto",
    accountFilter: "Filtruj kwarantannę według konta",
    allAccounts: "Wszystkie konta",
    excluded: "wyłączone",
    loading: "Wczytywanie kwarantanny…",
    error: "Nie udało się wczytać kwarantanny. Spróbuj ponownie.",
    empty: "Wszystkie wpisy są uwzględnione w sumach.",
    noDate: "Brak daty",
    noStatus: "Brak statusu",
    entries: (count: number) => `${String(count)} ${pluralPl(count, "wpis", "wpisy", "wpisów")}`,
    details: (count: number) => `Szczegóły wpisów (${String(count)})`,
    truncated: (shown: number, total: number) =>
      `Pokazano ${String(shown)} ${pluralPl(shown, "najnowszy wpis", "najnowsze wpisy", "najnowszych wpisów")} z ${String(total)} ${pluralPl(total, "wpisu", "wpisów", "wpisów")}.`,
    buckets: {
      uncertain: "Niepewne wpisy",
      unresolved: "Nierozstrzygnięte wpisy API",
      import_unresolved: "Nierozstrzygnięte wpisy importu",
    },
    statuses: {
      BOOK: "Zaksięgowany",
      PDNG: "Oczekujący",
      HOLD: "Wstrzymany",
      CNCL: "Anulowany",
      RJCT: "Odrzucony",
      SCHD: "Zaplanowany",
      OTHR: "Inny",
    },
    reasons: {
      non_booked_status: "Bank nie zaksięgował tego wpisu, dlatego nie jest on uwzględniony w sumach okresu.",
      missing_reference: "Wpis nie ma referencji bankowej, więc nie można go bezpiecznie dopasować.",
      missing_date: "Wpis nie ma daty księgowania ani daty waluty, więc nie można przypisać go do okresu.",
      reference_conflict: "Ta sama referencja bankowa wskazuje na sprzeczne dane wpisu, dlatego jest on przechowywany osobno.",
      non_canonical: "Import offline zawiera dane w nieobsługiwanym formacie, więc wpisu nie zastosowano.",
      api_preserved: "Bankowe API już dostarczyło ten wpis, dlatego import offline go nie zastąpił.",
      stale_conflict: "Import offline był sprzeczny z nowszymi zapisanymi danymi, więc go nie zastosowano.",
      same_time_conflict: "Sprzeczne wersje wpisu mają ten sam znacznik czasu, dlatego nie można bezpiecznie wybrać właściwej.",
    },
    unknownReason: (code: string) => `Wpis jest wyłączony z nieznanego powodu (${code}).`,
  },
};
export type QuarantineLabels = typeof quarantineLabels.en;

type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsQuarantine };

export interface QuarantineViewProps {
  api: Pick<BudgetApi, "quarantine">;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  text: QuarantineLabels;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
}

function reasonText(text: QuarantineLabels, code: string): string {
  return (text.reasons as Readonly<Record<string, string>>)[code] ?? text.unknownReason(code);
}

export function QuarantineView({ api, accounts, connections = [], text, revision, onAccessLoss }: QuarantineViewProps) {
  const [accountId, setAccountId] = useState("");
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setShown({ state: "loading" });
    void api.quarantine(accountId === "" ? undefined : accountId).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      if (!result.ok) {
        setShown({ state: "error" });
        return;
      }
      setShown({ state: "ready", data: result.data });
    });
  }, [api, accountId, revision, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const name = (id: string) => accountName(accounts, connections, id, text.account);

  return (
    <section className="budget-entries" aria-label={text.title}>
      <h4>{text.title}</h4>
      <p className="budget-muted">{text.intro}</p>
      <div className="budget-views" role="group" aria-label={text.accountFilter}>
        <button type="button" aria-pressed={accountId === ""} onClick={() => { setAccountId(""); }}>
          {text.allAccounts}
        </button>
        {accounts.map((item) => (
          <button type="button" key={item.id} aria-pressed={accountId === item.id} onClick={() => { setAccountId(item.id); }}>
            <span className="budget-entry-text">{name(item.id)}</span>
          </button>
        ))}
      </div>
      {shown.state === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {shown.state === "ready" ? (
        <>
          <section className="budget-total" aria-label={text.summary}>
            <h4>{text.summary}</h4>
            <dl>
              <dt>{text.buckets.uncertain}</dt><dd>{text.entries(shown.data.summary.uncertain_count)}</dd>
              <dt>{text.buckets.unresolved}</dt><dd>{text.entries(shown.data.summary.unresolved_count)}</dd>
              <dt>{text.buckets.import_unresolved}</dt><dd>{text.entries(shown.data.summary.import_unresolved_count)}</dd>
              <dt><strong>{text.total}</strong></dt><dd><strong>{text.entries(shown.data.summary.total_count)}</strong></dd>
            </dl>
          </section>
          <table className="budget-limits-table">
            <caption>{text.accounts}</caption>
            <thead>
              <tr>
                <th scope="col">{text.account}</th>
                <th scope="col">{text.buckets.uncertain}</th>
                <th scope="col">{text.buckets.unresolved}</th>
                <th scope="col">{text.buckets.import_unresolved}</th>
                <th scope="col">{text.total}</th>
              </tr>
            </thead>
            <tbody>
              {shown.data.accounts.map((item) => (
                <tr key={item.account_id} className={item.selected ? undefined : "is-excluded"}>
                  <th scope="row">
                    {name(item.account_id)}
                    {item.selected ? null : <small> ({text.excluded})</small>}
                  </th>
                  <td>{item.uncertain_count}</td>
                  <td>{item.unresolved_count}</td>
                  <td>{item.import_unresolved_count}</td>
                  <td>{item.total_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {shown.data.summary.total_count === 0 ? <p className="budget-muted">{text.empty}</p> : null}
          {shown.data.truncated ? (
            <p className="budget-notice" role="note">{text.truncated(shown.data.items.length, shown.data.summary.total_count)}</p>
          ) : null}
          {shown.data.items.length === 0 ? null : (
            <details className="budget-recurring-evidence">
              <summary>{text.details(shown.data.items.length)}</summary>
              <ul className="budget-entry-list">
                {shown.data.items.map((item, index) => (
                  <li className="budget-entry" key={`${item.account_id}/${item.entry_reference ?? ""}/${String(index)}`}>
                    <div className="budget-entry-facts">
                      <span>{item.date ?? text.noDate}</span>
                      <span className="budget-entry-text">{name(item.account_id)}</span>
                      <span className="budget-chip">{labelOf(text.buckets, item.bucket)}</span>
                      <span className="budget-chip">{item.status === null ? text.noStatus : labelOf(text.statuses, item.status)}</span>
                    </div>
                    <p className="budget-entry-text">{reasonText(text, item.reason)}</p>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </>
      ) : null}
    </section>
  );
}
