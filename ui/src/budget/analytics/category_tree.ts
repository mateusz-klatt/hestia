// Client-side category tree. Spending totals stay per category; parents show their own amount plus descendants.
import { formatScaledBigInt, parseScaledInt } from "./model";

export const MAX_CATEGORY_DEPTH = 3;

export interface CategoryLink {
  id: string;
  parent_id?: string | null;
  name: string;
  archived: boolean;
}

export interface CategoryAmount {
  category_id: string | null;
  name: string | null;
  archived: boolean;
  expenses: string;
  refunds: string;
  net_spending: string;
}

export interface CategoryTreeRow {
  category_id: string | null;
  name: string;
  archived: boolean;
  depth: number;
  parent_id: string | null;
  has_children: boolean;
  expenses: string;
  refunds: string;
  net_spending: string;
}

export interface CategoryListRow<T extends CategoryLink> {
  category: T;
  depth: number;
  has_children: boolean;
}

export interface ParentChoice {
  id: string;
  name: string;
  depth: number;
}

const ZERO = "0";

export function categoryParentId(category: { parent_id?: string | null }): string | null {
  return category.parent_id ?? null;
}

/** Exact decimal sum. Unparseable tokens contribute nothing, matching a missing amount. */
export function sumMoney(values: readonly string[]): string {
  let coeff = 0n;
  let scale = 0;
  for (const value of values) {
    const parsed = parseScaledInt(value);
    if (parsed === null) continue;
    const signed = parsed.negative ? -parsed.coeff : parsed.coeff;
    if (parsed.scale > scale) {
      coeff *= 10n ** BigInt(parsed.scale - scale);
      scale = parsed.scale;
    }
    coeff += signed * (10n ** BigInt(scale - parsed.scale));
  }
  return formatScaledBigInt(coeff, scale);
}

export function categoryParentMap(categories: readonly CategoryLink[]): Map<string, string | null> {
  return new Map(categories.map((item) => [item.id, categoryParentId(item)]));
}

function childrenMap(categories: readonly CategoryLink[]): Map<string, string[]> {
  const children = new Map<string, string[]>();
  const known = new Set(categories.map((item) => item.id));
  for (const item of categories) {
    const parent = categoryParentId(item);
    if (parent === null || !known.has(parent) || parent === item.id) continue;
    const list = children.get(parent) ?? [];
    list.push(item.id);
    children.set(parent, list);
  }
  return children;
}

export function categoryDepth(id: string, parents: ReadonlyMap<string, string | null>): number {
  let depth = 0;
  let current: string | null = id;
  const seen = new Set<string>();
  while (current !== null && !seen.has(current)) {
    seen.add(current);
    depth += 1;
    const parent = parents.get(current);
    current = parent === undefined ? null : parent;
  }
  return depth;
}

function subtreeHeight(id: string, children: ReadonlyMap<string, readonly string[]>): number {
  let height = 0;
  const stack: { id: string; dist: number }[] = [{ id, dist: 0 }];
  const seen = new Set<string>();
  while (stack.length > 0) {
    const item = stack.pop();
    if (item === undefined || seen.has(item.id)) continue;
    seen.add(item.id);
    if (item.dist > height) height = item.dist;
    for (const child of children.get(item.id) ?? []) stack.push({ id: child, dist: item.dist + 1 });
  }
  return height;
}

function blockedIds(subjectId: string | null, children: ReadonlyMap<string, readonly string[]>): Set<string> {
  const blocked = new Set<string>();
  if (subjectId === null) return blocked;
  const stack = [subjectId];
  while (stack.length > 0) {
    const id = stack.pop();
    if (id === undefined || blocked.has(id)) continue;
    blocked.add(id);
    stack.push(...(children.get(id) ?? []));
  }
  return blocked;
}

/** Active categories that can parent `subjectId` (null when creating) without a cycle or a fourth level. */
export function parentChoices(categories: readonly CategoryLink[], subjectId: string | null, locale: string): ParentChoice[] {
  const parents = categoryParentMap(categories);
  const children = childrenMap(categories);
  const blocked = blockedIds(subjectId, children);
  const height = subjectId === null ? 0 : subtreeHeight(subjectId, children);
  return categories
    .filter((item) => !item.archived && !blocked.has(item.id))
    .map((item) => ({ id: item.id, name: item.name, depth: categoryDepth(item.id, parents) }))
    .filter((item) => item.depth + 1 + height <= MAX_CATEGORY_DEPTH)
    .sort((left, right) => left.name.localeCompare(right.name, locale) || left.id.localeCompare(right.id));
}

function byName(locale: string, nameOf: (id: string) => string) {
  return (left: string, right: string) => nameOf(left).localeCompare(nameOf(right), locale) || left.localeCompare(right);
}

/** Every category, roots first, siblings by name. A missing parent is treated as a root. */
export function listCategoryTree<T extends CategoryLink>(categories: readonly T[], locale: string): CategoryListRow<T>[] {
  const byId = new Map(categories.map((item) => [item.id, item]));
  const children = childrenMap(categories);
  const nameOf = (id: string) => byId.get(id)?.name ?? id;
  const roots = categories
    .filter((item) => {
      const parent = categoryParentId(item);
      return parent === null || !byId.has(parent) || parent === item.id;
    })
    .map((item) => item.id)
    .sort(byName(locale, nameOf));
  const rows: CategoryListRow<T>[] = [];
  const walk = (id: string, depth: number) => {
    const category = byId.get(id);
    if (category === undefined) return;
    const childIds = [...(children.get(id) ?? [])].sort(byName(locale, nameOf));
    rows.push({ category, depth, has_children: childIds.length > 0 });
    for (const child of childIds) walk(child, depth + 1);
  };
  for (const id of roots) walk(id, 0);
  return rows;
}

interface Rolled { expenses: string; refunds: string; net: string }

/** Totals that have spending, plus ancestors, with each parent equal to itself plus descendants. */
export function groupCategoryTotals(
  totals: readonly CategoryAmount[],
  categories: readonly CategoryLink[],
  locale: string,
): CategoryTreeRow[] {
  const byId = new Map(categories.map((item) => [item.id, item]));
  const parents = categoryParentMap(categories);
  const children = childrenMap(categories);
  const direct = new Map<string, CategoryAmount>();
  const uncategorised: CategoryAmount[] = [];
  for (const row of totals) {
    if (row.category_id === null) uncategorised.push(row);
    else direct.set(row.category_id, row);
  }
  const needed = new Set<string>();
  for (const id of direct.keys()) {
    let current: string | null = id;
    const seen = new Set<string>();
    while (current !== null && !seen.has(current)) {
      seen.add(current);
      needed.add(current);
      const parent = parents.get(current);
      current = parent === undefined ? null : parent;
    }
  }
  const rolled = new Map<string, Rolled>();
  const roll = (id: string): Rolled => {
    const cached = rolled.get(id);
    if (cached !== undefined) return cached;
    const own = direct.get(id);
    const expenses = [own?.expenses ?? ZERO];
    const refunds = [own?.refunds ?? ZERO];
    const nets = [own?.net_spending ?? ZERO];
    for (const child of children.get(id) ?? []) {
      if (!needed.has(child)) continue;
      const sub = roll(child);
      expenses.push(sub.expenses);
      refunds.push(sub.refunds);
      nets.push(sub.net);
    }
    const result = { expenses: sumMoney(expenses), refunds: sumMoney(refunds), net: sumMoney(nets) };
    rolled.set(id, result);
    return result;
  };
  const nameOf = (id: string) => byId.get(id)?.name ?? direct.get(id)?.name ?? id;
  const roots = [...needed].filter((id) => {
    const parent = parents.get(id);
    return parent === undefined || parent === null || !needed.has(parent);
  }).sort(byName(locale, nameOf));
  const rows: CategoryTreeRow[] = [];
  const walk = (id: string, depth: number, parentId: string | null) => {
    const meta = byId.get(id);
    const own = direct.get(id);
    const total = roll(id);
    const childIds = [...(children.get(id) ?? [])].filter((child) => needed.has(child)).sort(byName(locale, nameOf));
    rows.push({
      category_id: id,
      name: meta?.name ?? own?.name ?? id,
      archived: meta?.archived ?? own?.archived ?? false,
      depth,
      parent_id: parentId,
      has_children: childIds.length > 0,
      expenses: total.expenses,
      refunds: total.refunds,
      net_spending: total.net,
    });
    for (const child of childIds) walk(child, depth + 1, id);
  };
  for (const id of roots) walk(id, 0, null);
  if (uncategorised.length > 0) {
    rows.push({
      category_id: null,
      name: "",
      archived: false,
      depth: 0,
      parent_id: null,
      has_children: false,
      expenses: sumMoney(uncategorised.map((row) => row.expenses)),
      refunds: sumMoney(uncategorised.map((row) => row.refunds)),
      net_spending: sumMoney(uncategorised.map((row) => row.net_spending)),
    });
  }
  return rows;
}

/** Hide a row when it sits under a collapsed ancestor. Rows must be in preorder. */
export function visibleCategoryRows(rows: readonly CategoryTreeRow[], collapsed: ReadonlySet<string>): CategoryTreeRow[] {
  const hidden = new Set<string>();
  const shown: CategoryTreeRow[] = [];
  for (const row of rows) {
    const parentHidden = row.parent_id !== null && (collapsed.has(row.parent_id) || hidden.has(row.parent_id));
    if (parentHidden) {
      if (row.category_id !== null) hidden.add(row.category_id);
      continue;
    }
    shown.push(row);
  }
  return shown;
}

/**
 * Bucket each amount onto the top-level category, or onto the direct child of `focusId`.
 * Uncategorised stays only at the top. Amounts outside the focus subtree are dropped.
 * Returns undefined for a target that should be skipped.
 */
export function rollupTarget(
  categoryId: string,
  parents: ReadonlyMap<string, string | null>,
  focusId: string | null,
): string | undefined {
  const chain = [categoryId];
  const seen = new Set<string>([categoryId]);
  let current = categoryId;
  for (;;) {
    const parent = parents.get(current);
    if (parent === undefined || parent === null || seen.has(parent)) break;
    seen.add(parent);
    chain.push(parent);
    current = parent;
  }
  if (focusId === null) return chain[chain.length - 1];
  const index = chain.indexOf(focusId);
  if (index === -1) return undefined;
  if (index === 0) return focusId;
  return chain[index - 1];
}

export function rollupNetAmounts(
  entries: readonly { category_id: string | null; net_spending: string }[],
  parents: ReadonlyMap<string, string | null>,
  focusId: string | null,
): { category_id: string | null; net_spending: string }[] {
  const buckets = new Map<string | null, string[]>();
  for (const entry of entries) {
    if (entry.category_id === null) {
      if (focusId !== null) continue;
      const list = buckets.get(null) ?? [];
      list.push(entry.net_spending);
      buckets.set(null, list);
      continue;
    }
    const target = rollupTarget(entry.category_id, parents, focusId);
    if (target === undefined) continue;
    const list = buckets.get(target) ?? [];
    list.push(entry.net_spending);
    buckets.set(target, list);
  }
  return [...buckets.entries()].map(([category_id, amounts]) => ({ category_id, net_spending: sumMoney(amounts) }));
}

export function hasCategoryParents(categories: readonly CategoryLink[]): boolean {
  return categories.some((item) => categoryParentId(item) !== null);
}
