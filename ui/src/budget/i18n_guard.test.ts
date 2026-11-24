import ts from "typescript";
import { describe, expect, it } from "vitest";

interface FsDirent {
  name: string;
  isDirectory(): boolean;
  isFile(): boolean;
}

interface FsModule {
  readdirSync(path: string, options: { withFileTypes: true }): FsDirent[];
  readFileSync(path: string, encoding: "utf8"): string;
}

interface PathModule {
  dirname(path: string): string;
  isAbsolute(path: string): boolean;
  join(...paths: string[]): string;
  relative(from: string, to: string): string;
}

interface UrlModule {
  fileURLToPath(url: string): string;
}

const fsSpecifier = "node:fs";
const pathSpecifier = "node:path";
const urlSpecifier = "node:url";
const fs = (await import(/* @vite-ignore */ fsSpecifier)) as FsModule;
const path = (await import(/* @vite-ignore */ pathSpecifier)) as PathModule;
const url = (await import(/* @vite-ignore */ urlSpecifier)) as UrlModule;

const POLISH_DIACRITIC = /[ĄĆĘŁŃÓŚŹŻąćęłńóśźż]/;
const LABEL_IDENTIFIER = /labels?$/i;
const USER_VISIBLE_ATTRS = new Set(["title", "aria-label", "placeholder", "alt"]);
const ALLOWED_BRAND_TOKEN = /(?<![\p{L}\p{N}_])(?:Vesta|Hestia|IBAN|BLIK)(?![\p{L}\p{N}_])/gu;
const ALLOWED_REMAINDER = /^[0-9\p{P}\p{S}\s]*$/u;
const ENTITY_PATTERN = /&(?:#(\d+)|#[xX]([0-9a-fA-F]+)|([a-zA-Z][a-zA-Z0-9]*));/g;
const BUDGET_ROOT = path.dirname(url.fileURLToPath(import.meta.url));

/**
 * Explicit allowlist for exact strings that are genuinely locale-invariant outside i18n.
 * ISO 4217 currency code input placeholder `"PLN"` stays identical across all locales.
 * Interpolated template keys join decoded static parts with `${}` and trim the result.
 */
const ALLOWED_LITERALS: Readonly<Record<string, readonly string[]>> = {
  "analytics/mortgages.tsx": ["PLN"],
  "analytics/spending_limits.tsx": ["PLN"],
};

const NAMED_HTML_ENTITIES: Readonly<Record<string, string>> = {
  Aogon: "Ą",
  aogon: "ą",
  Cacute: "Ć",
  cacute: "ć",
  Eogon: "Ę",
  eogon: "ę",
  Lstrok: "Ł",
  lstrok: "ł",
  Nacute: "Ń",
  nacute: "ń",
  Oacute: "Ó",
  oacute: "ó",
  Sacute: "Ś",
  sacute: "ś",
  Zacute: "Ź",
  zacute: "ź",
  Zdot: "Ż",
  zdot: "ż",
  amp: "&",
  lt: "<",
  gt: ">",
  quot: "\"",
  apos: "'",
  nbsp: "\u00a0",
  ensp: "\u2002",
  emsp: "\u2003",
  thinsp: "\u2009",
  mdash: "—",
  ndash: "–",
  minus: "−",
  hellip: "…",
  middot: "·",
  bull: "•",
  times: "×",
  deg: "°",
  percnt: "%",
  larr: "←",
  rarr: "→",
  uarr: "↑",
  darr: "↓",
  harr: "↔",
  laquo: "«",
  raquo: "»",
  bdquo: "„",
  rdquo: "”",
  ldquo: "“",
};

type ViolationRule =
  | "polish-literal"
  | "bilingual-object"
  | "label-locale-access"
  | "jsx-text"
  | "jsx-attr"
  | "plural-pl";

interface GuardViolation {
  file: string;
  line: number;
  rule: ViolationRule;
  detail: string;
}

function decodeHtmlEntities(raw: string): string {
  return raw.replace(ENTITY_PATTERN, (match, dec: string | undefined, hex: string | undefined, named: string | undefined) => {
    if (dec !== undefined) {
      const codePoint = Number.parseInt(dec, 10);
      return codePoint >= 0 && codePoint <= 0x10ffff ? String.fromCodePoint(codePoint) : match;
    }
    if (hex !== undefined) {
      const codePoint = Number.parseInt(hex, 16);
      return codePoint >= 0 && codePoint <= 0x10ffff ? String.fromCodePoint(codePoint) : match;
    }
    if (named !== undefined) {
      return NAMED_HTML_ENTITIES[named] ?? match;
    }
    return match;
  });
}

function isAllowedJsxText(raw: string): boolean {
  const decoded = decodeHtmlEntities(raw);
  if (decoded.trim() === "") return true;
  const withoutBrands = decoded.replace(ALLOWED_BRAND_TOKEN, " ");
  return ALLOWED_REMAINDER.test(withoutBrands);
}

function collectSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...collectSourceFiles(full));
    } else if (
      entry.isFile()
      && (entry.name.endsWith(".tsx") || entry.name.endsWith(".ts"))
      && !entry.name.endsWith(".test.ts")
      && !entry.name.endsWith(".test.tsx")
    ) {
      out.push(full);
    }
  }
  return out.sort();
}

function propertyKeyName(name: ts.PropertyName): string | null {
  if (ts.isIdentifier(name)) return name.text;
  if (ts.isStringLiteral(name) || ts.isNoSubstitutionTemplateLiteral(name)) return decodeHtmlEntities(name.text);
  return null;
}

function trailingIdentifierName(expr: ts.Expression): string | null {
  if (ts.isIdentifier(expr)) return expr.text;
  if (ts.isPropertyAccessExpression(expr)) return expr.name.text;
  return null;
}

type JsxLiteralBranch = ts.StringLiteral | ts.NoSubstitutionTemplateLiteral | ts.TemplateExpression;

function collectJsxLiteralBranches(expr: ts.Expression, out: JsxLiteralBranch[]): void {
  if (ts.isStringLiteral(expr) || ts.isNoSubstitutionTemplateLiteral(expr) || ts.isTemplateExpression(expr)) {
    out.push(expr);
    return;
  }
  if (ts.isConditionalExpression(expr)) {
    collectJsxLiteralBranches(expr.whenTrue, out);
    collectJsxLiteralBranches(expr.whenFalse, out);
    return;
  }
  if (ts.isParenthesizedExpression(expr)) {
    collectJsxLiteralBranches(expr.expression, out);
    return;
  }
  if (!ts.isBinaryExpression(expr)) return;

  const operator = expr.operatorToken.kind;
  if (operator === ts.SyntaxKind.BarBarToken || operator === ts.SyntaxKind.QuestionQuestionToken) {
    collectJsxLiteralBranches(expr.left, out);
  } else if (operator !== ts.SyntaxKind.AmpersandAmpersandToken) {
    return;
  }
  collectJsxLiteralBranches(expr.right, out);
}

export function checkBudgetSource(filePath: string, sourceText: string, rootDir = BUDGET_ROOT): GuardViolation[] {
  const isTsx = filePath.endsWith(".tsx");
  const scriptKind = isTsx ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sourceFile = ts.createSourceFile(filePath, sourceText, ts.ScriptTarget.Latest, true, scriptKind);
  const rel = (path.isAbsolute(filePath) ? path.relative(rootDir, filePath) : filePath).replaceAll("\\", "/");
  const allowed = new Set(ALLOWED_LITERALS[rel] ?? []);
  const hits: GuardViolation[] = [];

  const record = (node: ts.Node, rule: ViolationRule, detail: string) => {
    const { line } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
    hits.push({ file: rel, line: line + 1, rule, detail });
  };

  const checkJsxValue = (node: ts.Node, rule: "jsx-text" | "jsx-attr", raw: string, prefix = "") => {
    const decoded = decodeHtmlEntities(raw);
    const trimmed = decoded.trim();
    if (trimmed === "" || allowed.has(trimmed) || isAllowedJsxText(decoded)) return;
    record(node, rule, `${prefix}${JSON.stringify(decoded)}`);
  };

  const checkJsxExpression = (expr: ts.Expression, rule: "jsx-text" | "jsx-attr", prefix = "") => {
    const literals: JsxLiteralBranch[] = [];
    collectJsxLiteralBranches(expr, literals);
    for (const literal of literals) {
      if (!ts.isTemplateExpression(literal)) {
        checkJsxValue(literal, rule, literal.text, prefix);
        continue;
      }
      const parts = [literal.head.text, ...literal.templateSpans.map((span) => span.literal.text)];
      if (parts.every(isAllowedJsxText)) continue;
      const decoded = parts.map(decodeHtmlEntities).join("${}");
      if (!allowed.has(decoded.trim())) {
        record(literal, rule, `${prefix}${JSON.stringify(decoded)}`);
      }
    }
  };

  const visit = (node: ts.Node) => {
    if (ts.isObjectLiteralExpression(node)) {
      const keys = new Set<string>();
      for (const prop of node.properties) {
        if (ts.isPropertyAssignment(prop) || ts.isShorthandPropertyAssignment(prop) || ts.isMethodDeclaration(prop)) {
          const key = propertyKeyName(prop.name);
          if (key !== null) keys.add(key);
        }
      }
      if (keys.has("pl") && keys.has("en")) {
        record(node, "bilingual-object", "object literal contains both \"pl\" and \"en\" properties");
      }
    }

    if (ts.isPropertyAccessExpression(node) && (node.name.text === "pl" || node.name.text === "en")) {
      const target = trailingIdentifierName(node.expression);
      if (target !== null && LABEL_IDENTIFIER.test(target)) {
        record(node, "label-locale-access", `${target}.${node.name.text}`);
      }
    }

    if (
      ts.isElementAccessExpression(node)
      && (ts.isStringLiteral(node.argumentExpression) || ts.isNoSubstitutionTemplateLiteral(node.argumentExpression))
      && (node.argumentExpression.text === "pl" || node.argumentExpression.text === "en")
    ) {
      const target = trailingIdentifierName(node.expression);
      if (target !== null && LABEL_IDENTIFIER.test(target)) {
        record(node, "label-locale-access", `${target}[${JSON.stringify(node.argumentExpression.text)}]`);
      }
    }

    if (isTsx && ts.isIdentifier(node) && node.text === "pluralPl") {
      record(node, "plural-pl", "pluralPl");
    }

    if (isTsx) {
      if (
        ts.isStringLiteral(node)
        || ts.isNoSubstitutionTemplateLiteral(node)
        || ts.isTemplateHead(node)
        || ts.isTemplateMiddle(node)
        || ts.isTemplateTail(node)
        || ts.isJsxText(node)
      ) {
        const decoded = decodeHtmlEntities(node.text);
        if (POLISH_DIACRITIC.test(decoded) && !allowed.has(decoded)) {
          record(node, "polish-literal", JSON.stringify(decoded));
        }
      }

      if (ts.isJsxText(node)) {
        checkJsxValue(node, "jsx-text", node.text);
      }

      if (ts.isJsxExpression(node) && node.expression !== undefined
        && (ts.isJsxElement(node.parent) || ts.isJsxFragment(node.parent))) {
        checkJsxExpression(node.expression, "jsx-text");
      }

      if (ts.isJsxAttribute(node) && ts.isIdentifier(node.name) && USER_VISIBLE_ATTRS.has(node.name.text)
        && node.initializer !== undefined) {
        const prefix = `${node.name.text}=`;
        if (ts.isStringLiteral(node.initializer)) {
          checkJsxValue(node.initializer, "jsx-attr", node.initializer.text, prefix);
        } else if (ts.isJsxExpression(node.initializer) && node.initializer.expression !== undefined) {
          checkJsxExpression(node.initializer.expression, "jsx-attr", prefix);
        }
      }
    }

    ts.forEachChild(node, visit);
  };

  visit(sourceFile);
  return hits;
}

describe("budget UI i18n guard", () => {
  const sourceFiles = collectSourceFiles(BUDGET_ROOT);
  const tsxFiles = sourceFiles.filter((file) => file.endsWith(".tsx"));

  it("finds all non-test budget TS and TSX source files", () => {
    expect(tsxFiles.length).toBeGreaterThanOrEqual(19);
    expect(sourceFiles.length).toBeGreaterThanOrEqual(30);
  });

  it("enforces i18n guard rules across all non-test budget files", () => {
    const violations = sourceFiles.flatMap((file) => checkBudgetSource(file, fs.readFileSync(file, "utf8")));
    expect(violations).toEqual([]);
  });

  it("rejects bilingual label objects without diacritics and .pl/.en accesses on label identifiers", () => {
    const source = `
      const labels = { pl: { cancel: "Anuluj" }, en: { cancel: "Cancel" } };
      export const text = polish ? labels.pl : labels.en;
      export const direct = budgetLabel["pl"];
    `;
    const tsxViolations = checkBudgetSource("virtual.tsx", source);
    expect(tsxViolations.map((v) => v.rule)).toEqual([
      "bilingual-object",
      "label-locale-access",
      "label-locale-access",
      "label-locale-access",
    ]);

    const tsViolations = checkBudgetSource("virtual.ts", source);
    expect(tsViolations.map((v) => v.rule)).toEqual([
      "bilingual-object",
      "label-locale-access",
      "label-locale-access",
      "label-locale-access",
    ]);
  });

  it("rejects non-allowlisted Polish/English JSX text and user-visible attributes even without diacritics", () => {
    const jsxTextSource = "export function View() { return <button>Dodaj konto</button>; }";
    expect(checkBudgetSource("virtual.tsx", jsxTextSource).map((v) => v.rule)).toEqual(["jsx-text"]);

    const jsxExprSource = "export function View() { return <button>{\"Dodaj konto\"}</button>; }";
    expect(checkBudgetSource("virtual.tsx", jsxExprSource).map((v) => v.rule)).toEqual(["jsx-text"]);

    const attrSource = `
      export function Inputs() {
        return <input title="Kwota" aria-label="Dodaj konto" placeholder="Wpisz" alt="Ikona" />;
      }
    `;
    expect(checkBudgetSource("virtual.tsx", attrSource).map((v) => v.rule)).toEqual([
      "jsx-attr",
      "jsx-attr",
      "jsx-attr",
      "jsx-attr",
    ]);
  });

  it("decodes HTML entities before checking JSX text, attributes, and string literals", () => {
    const entityJsxSource = `
      export function EntityView() {
        const msg = "B&#322;&#261;d";
        return <button title="&aogon;" aria-label="B&#x142;&#261;d">&#261;</button>;
      }
    `;
    const violations = checkBudgetSource("virtual.tsx", entityJsxSource);
    expect(violations.map((v) => v.rule)).toEqual([
      "polish-literal",
      "jsx-attr",
      "polish-literal",
      "jsx-attr",
      "polish-literal",
      "polish-literal",
      "jsx-text",
    ]);
  });

  it("rejects text in template heads, middles, and tails in JSX children and visible attributes", () => {
    const source = `
      <>
        <span>{\`Total: \${amount}\`}</span>
        <span>{\`\${first} of \${last}\`}</span>
        <span>{\`\${count} it&#101;ms\`}</span>
        <input title={\`Total: \${amount}\`} aria-label={\`\${first} of \${last}\`}
          placeholder={\`\${count} it&#101;ms\`} alt={\`Account \${id}\`} />
      </>
    `;
    expect(checkBudgetSource("virtual.tsx", source).map(({ rule, detail }) => [rule, detail])).toEqual([
      ["jsx-text", '"Total: ${}"'],
      ["jsx-text", '"${} of ${}"'],
      ["jsx-text", '"${} items"'],
      ["jsx-attr", 'title="Total: ${}"'],
      ["jsx-attr", 'aria-label="${} of ${}"'],
      ["jsx-attr", 'placeholder="${} items"'],
      ["jsx-attr", 'alt="Account ${}"'],
    ]);
  });

  it("allows templates in class names and ids and locale-invariant template text", () => {
    const source = `
      <section className={\`budget-row \${selected}\`} id={\`budget-row-\${id}\`}>
        <span>{\`\${count} &times;\`}</span>
        <span>{\`\${used}/\${limit}\`}</span>
        <input title={\`Hestia \${version} · 100%\`} aria-label={\`\${account} IBAN\`}
          placeholder={\`\${month}-11\`} alt={\`\${count}\`} />
      </section>
    `;
    expect(checkBudgetSource("virtual.tsx", source)).toEqual([]);
  });

  it("rejects text in logical JSX branches in children and visible attributes", () => {
    const source = `
      <>
        {ready && "Ready"}
        {label || "Fallback"}
        {label ?? "Missing"}
        {("Heading") || label}
        {\`Entry \${id}\` ?? label}
        {(ready && "Loaded") || label}
        <input title={ready && (selected ? "Selected" : "Available")}
          aria-label={"Account" || "Fallback"} placeholder={label ?? "Enter a value"}
          alt={ready && \`Account \${id}\`} />
      </>
    `;
    expect(checkBudgetSource("virtual.tsx", source).map(({ rule, detail }) => [rule, detail])).toEqual([
      ["jsx-text", '"Ready"'],
      ["jsx-text", '"Fallback"'],
      ["jsx-text", '"Missing"'],
      ["jsx-text", '"Heading"'],
      ["jsx-text", '"Entry ${}"'],
      ["jsx-text", '"Loaded"'],
      ["jsx-attr", 'title="Selected"'],
      ["jsx-attr", 'title="Available"'],
      ["jsx-attr", 'aria-label="Account"'],
      ["jsx-attr", 'aria-label="Fallback"'],
      ["jsx-attr", 'placeholder="Enter a value"'],
      ["jsx-attr", 'alt="Account ${}"'],
    ]);
  });

  it("allows logical class names, ids, predicates, and locale-invariant display branches", () => {
    const source = `
      <section className={selected && "budget-row"} id={id ?? "budget-summary"}>
        {"predicate" && t("budget.main.title")}
        {status === "ready"}
        {label || "Vesta"}
        {symbol ?? "× 2"}
        <input title={ready && "IBAN"} aria-label={"BLIK" || label}
          placeholder={month ?? "2026-11"} alt={symbol ?? "…"} />
      </section>
    `;
    expect(checkBudgetSource("virtual.tsx", source)).toEqual([]);
    expect(checkBudgetSource("analytics/mortgages.tsx", '<input placeholder={currency ?? "PLN"} />')).toEqual([]);
  });

  it("allows t()/tPlural() calls alongside allowlisted punctuation, symbols, digits, and brand tokens", () => {
    const cleanSource = `
      export function CleanView({ name, count }: { name: string; count: number }) {
        return (
          <section aria-label={t("budget.main.title")} data-test="not-user-visible">
            <h4>Hestia · Vesta — 100% × 2 → ← ↑ ↓ ◀ ▶ ▸ ▾ ⚠</h4>
            <p>{name} (IBAN / BLIK)</p>
            <input placeholder="2026-11" title="IBAN" />
            <span>{tPlural("budget.overview.uncertainCount", count)}</span>
          </section>
        );
      }
    `;
    expect(checkBudgetSource("virtual.tsx", cleanSource)).toEqual([]);
  });
});
