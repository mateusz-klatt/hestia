// A period's entries with the owner's category/kind corrections (2D). Rows come from the server one bounded page
// at a time; a correction is an explicit, versioned decision, and a conflict reloads instead of overwriting.
import { useCallback, useEffect, useRef, useState } from "react";
import type { BudgetApi } from "../api";
import type { BudgetAccount } from "../model";
import { accountLabel, formatAmount, transactionDate, transactionText } from "../model";
import { KIND_CHOICES, NO_FILTER, UNCATEGORISED, annotationRequest, currentChoice } from "./model";
import type { AnalyticsCategory, AnalyticsTransaction, CategoryChoice, KindChoice, TransactionFilter } from "./model";

const kindsEn = { auto: "Automatic", expense: "Expense", income: "Income", refund: "Refund", excluded: "Leave out of the budget",
  unclassified_credit: "To classify", internal: "Own transfer" };
const kindsPl = { auto: "Automatycznie", expense: "Wydatek", income: "Wpływ", refund: "Zwrot", excluded: "Pomiń w budżecie",
  unclassified_credit: "Do sklasyfikowania", internal: "Przelew własny" };

export const transactionLabels = {
  en: {
    title: "Transactions", currency: "Currency", category: "Category", kind: "Type", search: "Search", apply: "Apply",
    all: "All", uncategorised: "Uncategorised", automatic: "Automatic (rules)", kinds: kindsEn,
    edit: "Change", save: "Save", cancel: "Cancel", total: (count: number) => `${String(count)} entries`,
    manual: "set by you", rule: "by rule", stale: "the bank changed this entry; check your choice",
    transfer: { none: "", suggested: "possible own transfer", accepted: "own transfer", review: "own transfer to re-check",
      rejected: "" },
    more: "Load more", empty: "No entries match.", loading: "Loading entries…", error: "Entries could not be loaded.",
    conflict: "This changed elsewhere; the latest state was loaded.", saveError: "Could not save. Try again.",
    account: "Account",
  },
  pl: {
    title: "Transakcje", currency: "Waluta", category: "Kategoria", kind: "Rodzaj", search: "Szukaj", apply: "Zastosuj",
    all: "Wszystkie", uncategorised: "Bez kategorii", automatic: "Automatycznie (reguły)", kinds: kindsPl,
    edit: "Zmień", save: "Zapisz", cancel: "Anuluj", total: (count: number) => `Pozycji: ${String(count)}`,
    manual: "ustawione przez Ciebie", rule: "z reguły", stale: "bank zmienił tę pozycję; sprawdź swój wybór",
    transfer: { none: "", suggested: "możliwy przelew własny", accepted: "przelew własny",
      review: "przelew własny do sprawdzenia", rejected: "" },
    more: "Wczytaj więcej", empty: "Brak pasujących pozycji.", loading: "Wczytywanie pozycji…",
    error: "Nie udało się wczytać pozycji.", conflict: "Zmieniono to w innym miejscu; wczytano aktualny stan.",
    saveError: "Nie udało się zapisać. Spróbuj ponownie.", account: "Konto",
  },
};
export type TransactionLabels = typeof transactionLabels.en;

const FILTER_KINDS = ["expense", "income", "refund", "unclassified_credit", "internal", "excluded"] as const;

interface Page { rows: AnalyticsTransaction[]; cursor: string | null; total: number; loading: boolean; failed: boolean }
const LOADING: Page = { rows: [], cursor: null, total: 0, loading: true, failed: false };

export interface TransactionsProps {
  api: Pick<BudgetApi, "analyticsTransactions" | "categories" | "annotate">;
  period: string;
  currencies: readonly string[];
  accounts: readonly BudgetAccount[];
  text: TransactionLabels;
  locale: string;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** A saved decision changes totals; the owner of this panel re-reads the period. */
  onChanged: () => void;
}

const rowKey = (row: AnalyticsTransaction) => `${row.account_id}/${row.entry_reference}`;

export function TransactionsPanel({ api, period, currencies, accounts, text, locale, revision, onAccessLoss, onChanged }: TransactionsProps) {
  const [filter, setFilter] = useState<TransactionFilter>(NO_FILTER);
  const [draft, setDraft] = useState<TransactionFilter>(NO_FILTER);
  const [page, setPage] = useState<Page>(LOADING);
  const [categories, setCategories] = useState<AnalyticsCategory[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reloads, setReloads] = useState(0);
  // Every period, filter or reload change takes a new generation; late pages and saves of an older one are dropped.
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setPage(LOADING);
    setEditing(null);
    setBusy(null);
    void api.categories().then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      if (result.ok) setCategories(result.data.categories);
    });
    void api.analyticsTransactions(period, filter).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      setPage(result.ok
        ? { rows: result.data.transactions, cursor: result.data.next_cursor, total: result.data.total, loading: false, failed: false }
        : { ...LOADING, loading: false, failed: true });
    });
  }, [api, period, filter, revision, reloads, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const more = useCallback(async () => {
    const token = generation.current;
    const cursor = page.cursor;
    if (cursor === null) return;
    setPage((current) => ({ ...current, loading: true }));
    const result = await api.analyticsTransactions(period, filter, cursor);
    if (onAccessLoss(result) || token !== generation.current) return;
    setPage((current) => (result.ok
      ? { rows: [...current.rows, ...result.data.transactions], cursor: result.data.next_cursor, total: result.data.total,
          loading: false, failed: false }
      : { ...current, loading: false, failed: true }));
  }, [api, period, filter, page.cursor, onAccessLoss]);

  const save = useCallback(async (row: AnalyticsTransaction, category: CategoryChoice, kind: KindChoice) => {
    const token = generation.current;
    setBusy(rowKey(row));
    setNotice(null);
    const result = await api.annotate(annotationRequest(row, category, kind));
    // The access barrier runs first, so a lost session clears even when the list moved on meanwhile.
    if (onAccessLoss(result) || token !== generation.current) return;
    setBusy(null);
    if (!result.ok) {
      setNotice(result.error === "budget_version_conflict" ? text.conflict : text.saveError);
      if (result.error === "budget_version_conflict") setReloads((current) => current + 1);
      return;
    }
    setEditing(null);
    onChanged();
  }, [api, onAccessLoss, onChanged, text]);

  const categoryName = (id: string | null) => (id === null ? text.uncategorised
    : categories.find((item) => item.id === id)?.name ?? text.uncategorised);
  const accountName = (id: string) => {
    const account = accounts.find((item) => item.id === id);
    if (account === undefined) return text.account;
    return accountLabel(account, accounts.filter((item) => item.connection === account.connection).indexOf(account), text.account);
  };
  const kindText = (kind: string) => (text.kinds as Record<string, string>)[kind] ?? kind;

  return (
    <section className="budget-entries" aria-label={text.title}>
      <h4>{text.title}</h4>
      <form className="budget-range" onSubmit={(event) => { event.preventDefault(); setFilter(draft); }}>
        <label>{text.currency}
          <select value={draft.currency} onChange={(event) => { setDraft({ ...draft, currency: event.currentTarget.value }); }}>
            <option value="">{text.all}</option>
            {currencies.map((currency) => <option key={currency} value={currency}>{currency}</option>)}
          </select>
        </label>
        <label>{text.category}
          <select value={draft.category} onChange={(event) => { setDraft({ ...draft, category: event.currentTarget.value }); }}>
            <option value="">{text.all}</option>
            <option value={UNCATEGORISED}>{text.uncategorised}</option>
            {categories.filter((item) => !item.archived).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <label>{text.kind}
          <select value={draft.kind} onChange={(event) => { setDraft({ ...draft, kind: event.currentTarget.value }); }}>
            <option value="">{text.all}</option>
            {FILTER_KINDS.map((kind) => <option key={kind} value={kind}>{kindText(kind)}</option>)}
          </select>
        </label>
        <label>{text.search}
          <input type="search" maxLength={100} value={draft.query}
            onChange={(event) => { setDraft({ ...draft, query: event.currentTarget.value }); }} />
        </label>
        <button type="submit">{text.apply}</button>
      </form>
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {page.failed ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {page.loading && page.rows.length === 0 ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {!page.loading && !page.failed && page.rows.length === 0 ? <p className="budget-muted">{text.empty}</p> : null}
      {page.rows.length === 0 ? null : (
        <>
          <p className="budget-muted">{text.total(page.total)}</p>
          <ul className="budget-entry-list">
            {page.rows.map((row) => {
              const key = rowKey(row);
              const transfer = text.transfer[row.transfer_state];
              return (
                <li key={key} className="budget-entry">
                  <div className="budget-entry-main">
                    <span className="budget-muted">{transactionDate(row)}</span>
                    <span className="budget-entry-text">{row.counterparty_name ?? transactionText(row)}</span>
                    <strong className={row.credit_debit_indicator === "DBIT" ? "is-debit" : "is-credit"}>
                      {formatAmount(row.amount, row.currency, locale, row.credit_debit_indicator)}
                    </strong>
                  </div>
                  <div className="budget-entry-facts">
                    <span>{accountName(row.account_id)}</span>
                    <span className="budget-chip">{categoryName(row.effective.category_id)}</span>
                    {row.effective.kind === "expense" || row.effective.kind === "income" ? null
                      : <span className="budget-chip">{kindText(row.effective.kind)}</span>}
                    {row.effective.source === "manual" ? <span>{text.manual}</span> : null}
                    {row.effective.source === "rule" ? <span>{text.rule}</span> : null}
                    {transfer === "" ? null : <span className="budget-chip">{transfer}</span>}
                    {row.annotation?.stale === true ? <span className="budget-warn">{text.stale}</span> : null}
                    {editing === key ? null : (
                      <button type="button" disabled={busy !== null} onClick={() => { setEditing(key); setNotice(null); }}>{text.edit}</button>
                    )}
                  </div>
                  {editing === key ? (
                    <Editor row={row} categories={categories} text={text} kindText={kindText} saving={busy === key}
                      onSave={(category, kind) => { void save(row, category, kind); }} onCancel={() => { setEditing(null); }} />
                  ) : null}
                </li>
              );
            })}
          </ul>
        </>
      )}
      {page.cursor === null ? null
        : <button type="button" disabled={page.loading} onClick={() => { void more(); }}>{text.more}</button>}
    </section>
  );
}

interface EditorProps {
  row: AnalyticsTransaction;
  categories: readonly AnalyticsCategory[];
  text: TransactionLabels;
  kindText: (kind: string) => string;
  saving: boolean;
  onSave: (category: CategoryChoice, kind: KindChoice) => void;
  onCancel: () => void;
}

/** Select values: "auto" (rules decide), "none" (explicitly uncategorised) or "cat:<id>". */
function encode(choice: CategoryChoice): string {
  if (choice.mode === "auto") return "auto";
  return choice.category_id === null ? "none" : `cat:${choice.category_id}`;
}
function decode(value: string): CategoryChoice {
  if (value === "auto") return { mode: "auto" };
  return { mode: "manual", category_id: value.startsWith("cat:") ? value.slice(4) : null };
}

function Editor({ row, categories, text, kindText, saving, onSave, onCancel }: EditorProps) {
  const [category, setCategory] = useState(encode(currentChoice(row)));
  const [kind, setKind] = useState<KindChoice>(row.annotation?.kind ?? "auto");
  const chosen = row.annotation?.category_id ?? null;
  // An archived category stays selectable only for the entry that already uses it.
  const offered = categories.filter((item) => !item.archived || item.id === chosen);
  return (
    <form className="budget-entry-editor" onSubmit={(event) => { event.preventDefault(); onSave(decode(category), kind); }}>
      <label>{text.category}
        <select value={category} onChange={(event) => { setCategory(event.currentTarget.value); }}>
          <option value="auto">{text.automatic}</option>
          <option value="none">{text.uncategorised}</option>
          {offered.map((item) => <option key={item.id} value={`cat:${item.id}`}>{item.name}</option>)}
        </select>
      </label>
      <label>{text.kind}
        <select value={kind} onChange={(event) => { setKind(event.currentTarget.value as KindChoice); }}>
          {KIND_CHOICES.map((item) => <option key={item} value={item}>{kindText(item)}</option>)}
        </select>
      </label>
      <button type="submit" className="budget-primary" disabled={saving}>{text.save}</button>
      <button type="button" disabled={saving} onClick={onCancel}>{text.cancel}</button>
    </form>
  );
}
