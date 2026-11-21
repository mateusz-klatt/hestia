// A period's entries with the owner's category/kind corrections (2D). Rows come from the server one bounded page
// at a time; a correction is an explicit, versioned decision, and a conflict reloads instead of overwriting.
import { useCallback, useEffect, useRef, useState } from "react";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { formatAmount, transactionDate, transactionText } from "../model";
import { ANALYTICS_ERROR, EFFECTIVE_KINDS, NO_FILTER, RELOAD_ERRORS, UNCATEGORISED, annotationRequest, canPair, counterparty,
  accountName as nameOf, currentChoice, daysBetween, kindChoice, kindChoicesFor, labelOf, manualPairRequest, pairCandidates,
  pairDate, pairPeriods } from "./model";
import type { AnalyticsCategory, AnalyticsTransaction, CategoryChoice, KindChoice, TransactionFilter } from "./model";

const kindsEn = { auto: "Automatic", expense: "Expense", income: "Income", refund: "Refund", excluded: "Leave out of the budget",
  unclassified_credit: "To classify", transfer: "Own transfer" };
const kindsPl = { auto: "Automatycznie", expense: "Wydatek", income: "Wpływ", refund: "Zwrot", excluded: "Pomiń w budżecie",
  unclassified_credit: "Do sklasyfikowania", transfer: "Przelew własny" };

export const transactionLabels = {
  en: {
    title: "Transactions", currency: "Currency", category: "Category", kind: "Type", search: "Search", apply: "Apply",
    all: "All", uncategorised: "Uncategorised", automatic: "Automatic (rules)", kinds: kindsEn,
    edit: "Change", save: "Save", cancel: "Cancel", archived: "archived", total: (count: number) => `${String(count)} entries`,
    manual: "set by you", rule: "by rule", stale: "the bank changed this entry; check your choice",
    transfer: { none: "", suggested: "possible own transfer", accepted: "own transfer", review: "own transfer to re-check",
      boundary: "other side on an excluded account", rejected: "", undone: "" },
    ambiguous: "fits several pairs; choose in own transfers",
    more: "Load more", empty: "No entries match.", loading: "Loading entries…", error: "Entries could not be loaded.",
    conflict: "This changed elsewhere; the latest state was loaded.", saveError: "Could not save. Try again.",
    changed: "The bank changed this entry; the latest state was loaded.", account: "Account",
    pair: "Pair as own transfer", pairTitle: "Matching credits within 3 days", pairLoading: "Looking for matching entries…",
    pairEmpty: "No matching credit within 3 days on another account.",
    pairLimited: "Only part of the period was searched; some entries may be missing.", pairError: "Matching entries could not be loaded.",
    pairConfirm: "Confirm this pair",
    pairDays: (count: number) => (count === 0 ? "same day" : `${String(count)} ${count === 1 ? "day" : "days"} apart`),
    pairReserved: "One of these entries is already in another pair; the latest state was loaded.",
    pairExisting: "These two entries already have a decision; see the own-transfer queues.",
    pairInvalid: "These entries cannot be paired (amount, currency, dates or account differ).",
  },
  pl: {
    title: "Transakcje", currency: "Waluta", category: "Kategoria", kind: "Rodzaj", search: "Szukaj", apply: "Zastosuj",
    all: "Wszystkie", uncategorised: "Bez kategorii", automatic: "Automatycznie (reguły)", kinds: kindsPl,
    edit: "Zmień", save: "Zapisz", cancel: "Anuluj", archived: "archiwalna", total: (count: number) => `Pozycji: ${String(count)}`,
    manual: "ustawione przez Ciebie", rule: "z reguły", stale: "bank zmienił tę pozycję; sprawdź swój wybór",
    transfer: { none: "", suggested: "możliwy przelew własny", accepted: "przelew własny",
      review: "przelew własny do sprawdzenia", boundary: "druga strona na koncie wykluczonym", rejected: "", undone: "" },
    ambiguous: "pasuje do kilku par; wybierz w przelewach własnych",
    more: "Wczytaj więcej", empty: "Brak pasujących pozycji.", loading: "Wczytywanie pozycji…",
    error: "Nie udało się wczytać pozycji.", conflict: "Zmieniono to w innym miejscu; wczytano aktualny stan.",
    saveError: "Nie udało się zapisać. Spróbuj ponownie.", changed: "Bank zmienił tę pozycję; wczytano aktualny stan.",
    account: "Konto",
    pair: "Paruj jako przelew własny", pairTitle: "Pasujące wpływy w ciągu 3 dni", pairLoading: "Szukam pasujących pozycji…",
    pairEmpty: "Brak pasującego wpływu w ciągu 3 dni na innym koncie.",
    pairLimited: "Przeszukano tylko część okresu; części pozycji może brakować.", pairError: "Nie udało się wczytać pasujących pozycji.",
    pairConfirm: "Potwierdź tę parę",
    pairDays: (count: number) => (count === 0 ? "ten sam dzień" : `${String(count)} ${count === 1 ? "dzień" : "dni"} różnicy`),
    pairReserved: "Jedna z tych pozycji jest już w innej parze; wczytano aktualny stan.",
    pairExisting: "Te dwie pozycje mają już decyzję; zobacz kolejki przelewów własnych.",
    pairInvalid: "Tych pozycji nie można sparować (różnią się kwota, waluta, daty lub konto).",
  },
};
export type TransactionLabels = typeof transactionLabels.en;

interface Page { rows: AnalyticsTransaction[]; cursor: string | null; total: number; loading: boolean; failed: boolean }
const LOADING: Page = { rows: [], cursor: null, total: 0, loading: true, failed: false };
interface PairPage { rows: AnalyticsTransaction[]; loading: boolean; failed: boolean; limited: boolean }
const PAIR_LOADING: PairPage = { rows: [], loading: true, failed: false, limited: false };

export interface TransactionsProps {
  api: Pick<BudgetApi, "analyticsTransactions" | "categories" | "annotate" | "decideTransfer">;
  period: string;
  currencies: readonly string[];
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  text: TransactionLabels;
  locale: string;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** A saved decision changes totals; the owner of this panel re-reads the period. */
  onChanged: () => void;
}

const rowKey = (row: AnalyticsTransaction) => `${row.account_id}/${row.entry_reference}`;

export function TransactionsPanel({ api, period, currencies, accounts, connections = [], text, locale, revision, onAccessLoss,
  onChanged }: TransactionsProps) {
  const [filter, setFilter] = useState<TransactionFilter>(NO_FILTER);
  const [draft, setDraft] = useState<TransactionFilter>(NO_FILTER);
  const [page, setPage] = useState<Page>(LOADING);
  const [categories, setCategories] = useState<AnalyticsCategory[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [pairing, setPairing] = useState<string | null>(null);
  const [pairPage, setPairPage] = useState<PairPage>(PAIR_LOADING);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reloads, setReloads] = useState(0);
  // Every period, filter or reload change takes a new generation; late pages and saves of an older one are dropped.
  const generation = useRef(0);
  const picker = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    picker.current += 1;
    setPage(LOADING);
    setEditing(null);
    setPairing(null);
    setBusy(null);
    void api.categories().then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      if (result.ok) setCategories(result.data.categories);
    });
    void api.analyticsTransactions(period, filter).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      setPage(result.ok
        ? { rows: result.data.transactions, cursor: result.data.next_cursor, total: result.data.filtered_total, loading: false, failed: false }
        : { ...LOADING, loading: false, failed: true });
    });
  }, [api, period, filter, revision, reloads, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; picker.current += 1; }, []);

  const more = useCallback(async () => {
    const token = generation.current;
    const cursor = page.cursor;
    if (cursor === null) return;
    setPage((current) => ({ ...current, loading: true }));
    const result = await api.analyticsTransactions(period, filter, cursor);
    if (onAccessLoss(result) || token !== generation.current) return;
    // The data moved under the cursor: start again from the first page rather than mixing two snapshots.
    if (!result.ok && result.error === ANALYTICS_ERROR.staleCursor) { setReloads((current) => current + 1); return; }
    setPage((current) => (result.ok
      ? { rows: [...current.rows, ...result.data.transactions], cursor: result.data.next_cursor, total: result.data.filtered_total,
          loading: false, failed: false }
      : { ...current, loading: false, failed: true }));
  }, [api, period, filter, page.cursor, onAccessLoss]);

  const loadPairs = useCallback(async (debit: AnalyticsTransaction, pickerToken: number, generationToken: number) => {
    const date = pairDate(debit);
    if (date === null) return;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      if (generationToken !== generation.current || pickerToken !== picker.current) return;
      setPairPage(PAIR_LOADING);
      const rows: AnalyticsTransaction[] = [];
      let limited = false;
      let restart = false;
      for (const month of pairPeriods(date)) {
        let cursor: string | undefined;
        for (let pageNumber = 0; pageNumber < 10; pageNumber += 1) {
          const result = await api.analyticsTransactions(month,
            { currency: debit.currency, category: "", kind: "", query: "" }, cursor, 200);
          if (onAccessLoss(result) || generationToken !== generation.current || pickerToken !== picker.current) return;
          if (!result.ok) {
            if (result.error === ANALYTICS_ERROR.staleCursor && attempt === 0) { restart = true; break; }
            setPairPage({ rows: [], loading: false, failed: true, limited: false });
            return;
          }
          rows.push(...result.data.transactions);
          cursor = result.data.next_cursor ?? undefined;
          if (cursor === undefined) break;
          if (pageNumber === 9) limited = true;
        }
        if (restart) break;
      }
      if (restart) continue;
      setPairPage({ rows, loading: false, failed: false, limited });
      return;
    }
  }, [api, onAccessLoss]);

  const closePairs = useCallback(() => {
    picker.current += 1;
    setPairing(null);
  }, []);

  const openPairs = useCallback((row: AnalyticsTransaction) => {
    if (!canPair(row)) return;
    const pickerToken = ++picker.current;
    setPairing(rowKey(row));
    setPairPage(PAIR_LOADING);
    setEditing(null);
    setNotice(null);
    void loadPairs(row, pickerToken, generation.current);
  }, [loadPairs]);

  const save = useCallback(async (row: AnalyticsTransaction, category: CategoryChoice, kind: KindChoice) => {
    const token = generation.current;
    setBusy(rowKey(row));
    setNotice(null);
    const result = await api.annotate(annotationRequest(row, category, kind));
    // The access barrier runs first, so a lost session clears even when the list moved on meanwhile.
    const accessLost = onAccessLoss(result);
    if (!accessLost && result.ok) onChanged();
    if (token !== generation.current) return;
    setBusy(null);
    if (accessLost) return;
    if (!result.ok) {
      setNotice(result.error === ANALYTICS_ERROR.sourceChanged ? text.changed
        : RELOAD_ERRORS.has(result.error) ? text.conflict : text.saveError);
      if (RELOAD_ERRORS.has(result.error)) setReloads((current) => current + 1);
      return;
    }
    setEditing(null);
  }, [api, onAccessLoss, onChanged, text]);

  const confirmPair = useCallback(async (debit: AnalyticsTransaction, credit: AnalyticsTransaction) => {
    const generationToken = generation.current;
    const pickerToken = picker.current;
    setBusy(`${rowKey(debit)}>${rowKey(credit)}`);
    setNotice(null);
    const result = await api.decideTransfer(manualPairRequest(debit, credit));
    const accessLost = onAccessLoss(result);
    if (!accessLost && result.ok) onChanged();
    if (generationToken !== generation.current || pickerToken !== picker.current) return;
    setBusy(null);
    if (accessLost) return;
    if (!result.ok) {
      const reserved = result.error === ANALYTICS_ERROR.legReserved;
      const existing = result.error === ANALYTICS_ERROR.conflict;
      setNotice(result.error === ANALYTICS_ERROR.sourceChanged ? text.changed
        : reserved ? text.pairReserved : existing ? text.pairExisting
          : result.error === ANALYTICS_ERROR.invalidRequest ? text.pairInvalid
            : RELOAD_ERRORS.has(result.error) ? text.conflict : text.saveError);
      if (RELOAD_ERRORS.has(result.error)) setReloads((current) => current + 1);
      return;
    }
    picker.current += 1;
    setPairing(null);
  }, [api, onAccessLoss, onChanged, text]);

  const categoryName = (id: string | null) => (id === null ? text.uncategorised
    : categories.find((item) => item.id === id)?.name ?? text.uncategorised);
  const accountName = (id: string) => nameOf(accounts, connections, id, text.account);
  const kindText = (kind: string) => labelOf(text.kinds, kind);

  return (
    <section className="budget-entries" aria-label={text.title}>
      <h4>{text.title}</h4>
      <form className="budget-range" onSubmit={(event) => { event.preventDefault(); setFilter(draft); }}>
        <label>{text.currency}
          <select name="currency" value={draft.currency} onChange={(event) => { setDraft({ ...draft, currency: event.currentTarget.value }); }}>
            <option value="">{text.all}</option>
            {currencies.map((currency) => <option key={currency} value={currency}>{currency}</option>)}
          </select>
        </label>
        <label>{text.category}
          <select name="category" value={draft.category} onChange={(event) => { setDraft({ ...draft, category: event.currentTarget.value }); }}>
            <option value="">{text.all}</option>
            <option value={UNCATEGORISED}>{text.uncategorised}</option>
            {categories.filter((item) => !item.archived).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <label>{text.kind}
          <select name="kind" value={draft.kind} onChange={(event) => { setDraft({ ...draft, kind: event.currentTarget.value }); }}>
            <option value="">{text.all}</option>
            {EFFECTIVE_KINDS.map((kind) => <option key={kind} value={kind}>{kindText(kind)}</option>)}
          </select>
        </label>
        <label>{text.search}
          <input name="query" type="search" maxLength={100} value={draft.query}
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
              const transfer = labelOf(text.transfer, row.transfer.status === "none" ? "" : row.transfer.status);
              return (
                <li key={key} className="budget-entry">
                  <div className="budget-entry-main">
                    <span className="budget-muted">{transactionDate(row)}</span>
                    <span className="budget-entry-text">{counterparty(row) ?? transactionText(row)}</span>
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
                    {row.transfer.ambiguous ? <span className="budget-warn">{text.ambiguous}</span> : null}
                    {row.annotation.stale ? <span className="budget-warn">{text.stale}</span> : null}
                    {editing === key ? null : (
                      <button type="button" disabled={busy !== null} onClick={() => { closePairs(); setEditing(key); setNotice(null); }}>{text.edit}</button>
                    )}
                    {canPair(row) && pairing !== key ? (
                      <button type="button" disabled={busy !== null} onClick={() => { openPairs(row); }}>{text.pair}</button>
                    ) : null}
                  </div>
                  {editing === key ? (
                    <Editor row={row} categories={categories} text={text} kindText={kindText} saving={busy === key}
                      onSave={(category, kind) => { void save(row, category, kind); }} onCancel={() => { setEditing(null); }} />
                  ) : null}
                  {pairing === key ? (
                    <PairPicker debit={row} page={pairPage} accounts={accounts} connections={connections} text={text} locale={locale}
                      disabled={busy !== null} onConfirm={(credit) => { void confirmPair(row, credit); }} onCancel={closePairs} />
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

interface PairPickerProps {
  debit: AnalyticsTransaction;
  page: PairPage;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  text: TransactionLabels;
  locale: string;
  disabled: boolean;
  onConfirm: (credit: AnalyticsTransaction) => void;
  onCancel: () => void;
}

function PairPicker({ debit, page, accounts, connections, text, locale, disabled, onConfirm, onCancel }: PairPickerProps) {
  const candidates = pairCandidates(debit, page.rows);
  const debitDate = pairDate(debit);
  return (
    <div className="budget-pair-picker" aria-label={text.pairTitle}>
      <p className="budget-muted">{text.pairTitle}</p>
      {page.limited ? <p className="budget-notice" role="note">{text.pairLimited}</p> : null}
      {page.loading ? <p className="budget-muted" role="status">{text.pairLoading}</p> : null}
      {page.failed ? <p className="budget-notice" role="alert">{text.pairError}</p> : null}
      {!page.loading && !page.failed && candidates.length === 0 ? <p className="budget-muted">{text.pairEmpty}</p> : null}
      {!page.loading && !page.failed && candidates.length > 0 ? (
        <ul className="budget-entry-list">
          {candidates.map((credit) => {
            const creditDate = pairDate(credit);
            if (debitDate === null || creditDate === null) return null;
            return (
              <li key={rowKey(credit)} className="budget-entry">
                <div className="budget-entry-main">
                  <span className="budget-muted">{creditDate}</span>
                  <span className="budget-entry-text">{counterparty(credit) ?? transactionText(credit)}</span>
                  <strong className="is-credit">{formatAmount(credit.amount, credit.currency, locale, credit.credit_debit_indicator)}</strong>
                </div>
                <div className="budget-entry-facts">
                  <span>{nameOf(accounts, connections, credit.account_id, text.account)}</span>
                  <span>{text.pairDays(daysBetween(debitDate, creditDate))}</span>
                  <button type="button" className="budget-primary" disabled={disabled} onClick={() => { onConfirm(credit); }}>{text.pairConfirm}</button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
      <div><button type="button" disabled={disabled} onClick={onCancel}>{text.cancel}</button></div>
    </div>
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
  const kinds = kindChoicesFor(row.credit_debit_indicator);
  const [kind, setKind] = useState<KindChoice>(row.annotation.mode === "manual"
    ? kindChoice(row.annotation.kind, row.credit_debit_indicator) : "auto");
  // An archived category stays selectable only as this entry's current classification: the category it shows now,
  // or its own manual choice unless the bank changed the entry since. Assigning any other archived category is refused.
  const manual = row.annotation.mode === "manual" && !row.annotation.stale ? row.annotation.category_id : null;
  const kept = new Set([manual, row.effective.category_id]);
  const offered = categories.filter((item) => !item.archived || kept.has(item.id));
  // "Automatic" hands both category and type back to rules. Choosing a type is a manual decision, so the category
  // visibly becomes the one the entry shows now (or explicitly none) instead of a hidden automatic state.
  const chooseKind = (value: KindChoice) => {
    setKind(value);
    if (value !== "auto" && category === "auto") setCategory(encode({ mode: "manual", category_id: row.effective.category_id }));
  };
  const chooseCategory = (value: string) => {
    setCategory(value);
    if (value === "auto") setKind("auto");
  };
  return (
    <form className="budget-entry-editor" onSubmit={(event) => { event.preventDefault(); onSave(decode(category), kind); }}>
      <label>{text.category}
        <select name="category_id" value={category} disabled={saving} onChange={(event) => { chooseCategory(event.currentTarget.value); }}>
          <option value="auto">{text.automatic}</option>
          <option value="none">{text.uncategorised}</option>
          {offered.map((item) => (
            <option key={item.id} value={`cat:${item.id}`}>{item.archived ? `${item.name} (${text.archived})` : item.name}</option>
          ))}
        </select>
      </label>
      <label>{text.kind}
        <select name="kind" value={kind} disabled={saving}
          onChange={(event) => { chooseKind(kindChoice(event.currentTarget.value, row.credit_debit_indicator)); }}>
          {kinds.map((item) => <option key={item} value={item}>{kindText(item)}</option>)}
        </select>
      </label>
      <button type="submit" className="budget-primary" disabled={saving}>{text.save}</button>
      <button type="button" disabled={saving} onClick={onCancel}>{text.cancel}</button>
    </form>
  );
}
