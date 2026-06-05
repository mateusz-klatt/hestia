/* eslint-disable @typescript-eslint/no-unnecessary-type-parameters */
/** Test-only DOM helpers (imported by *.test.ts only; tree-shaken from the app build). They throw on
 *  a missing element so a test fails loudly instead of leaning on a `!` non-null assertion. The caller
 *  supplies the element type explicitly (e.g. `q<HTMLInputElement>(...)`), so the generic is by design. */

export function q<T extends Element>(root: ParentNode, selector: string): T {
  const el = root.querySelector<T>(selector);
  if (el === null) throw new Error(`missing element: ${selector}`);
  return el;
}

export function qa<T extends Element>(root: ParentNode, selector: string): T[] {
  return [...root.querySelectorAll<T>(selector)];
}

export function nth<T extends Element>(root: ParentNode, selector: string, index: number): T {
  const el = qa<T>(root, selector)[index];
  if (el === undefined) throw new Error(`missing element: ${selector}[${String(index)}]`);
  return el;
}
