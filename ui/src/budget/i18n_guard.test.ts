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
const BUDGET_ROOT = path.dirname(url.fileURLToPath(import.meta.url));

/** Explicit allowlist if a budget `.tsx` file ever needs a literal with Polish diacritics outside i18n. */
const ALLOWED_LITERALS: Readonly<Record<string, readonly string[]>> = {};

function collectTsxFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...collectTsxFiles(full));
    } else if (entry.isFile() && entry.name.endsWith(".tsx") && !entry.name.endsWith(".test.tsx")) {
      out.push(full);
    }
  }
  return out.sort();
}

function findPolishLiterals(filePath: string, sourceText: string): Array<{ line: number; text: string }> {
  const sourceFile = ts.createSourceFile(filePath, sourceText, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const rel = path.relative(BUDGET_ROOT, filePath).replaceAll("\\", "/");
  const allowed = new Set(ALLOWED_LITERALS[rel] ?? []);
  const hits: Array<{ line: number; text: string }> = [];

  const check = (node: ts.Node, text: string) => {
    if (!POLISH_DIACRITIC.test(text) || allowed.has(text)) return;
    const { line } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
    hits.push({ line: line + 1, text });
  };

  const visit = (node: ts.Node) => {
    if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) ||
      ts.isTemplateTail(node) ||
      ts.isJsxText(node)
    ) {
      check(node, node.text);
    }
    ts.forEachChild(node, visit);
  };

  visit(sourceFile);
  return hits;
}

describe("budget UI i18n guard", () => {
  const tsxFiles = collectTsxFiles(BUDGET_ROOT);

  it("finds all non-test budget TSX source files", () => {
    expect(tsxFiles.length).toBeGreaterThanOrEqual(19);
  });

  it("forbids Polish-diacritic string literals and JSX text outside the catalog", () => {
    const violations: string[] = [];
    for (const file of tsxFiles) {
      const content = fs.readFileSync(file, "utf8");
      const rel = path.relative(BUDGET_ROOT, file).replaceAll("\\", "/");
      for (const hit of findPolishLiterals(file, content)) {
        violations.push(`${rel}:${String(hit.line)} -> ${JSON.stringify(hit.text)}`);
      }
    }
    expect(violations).toEqual([]);
  });

  it("ensures budget view TSX components use the central i18n helpers instead of pluralPl", () => {
    const pluralPlHits: string[] = [];
    for (const file of tsxFiles) {
      const content = fs.readFileSync(file, "utf8");
      const rel = path.relative(BUDGET_ROOT, file).replaceAll("\\", "/");
      if (/\bpluralPl\b/.test(content)) {
        pluralPlHits.push(rel);
      }
    }
    expect(pluralPlHits).toEqual([]);
  });
});
