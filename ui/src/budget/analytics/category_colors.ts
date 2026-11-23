// Pure category colour assignment, OKLCH shading, hatched patterns, and WCAG AA badge text contrast.
// No React, DOM, or backend dependencies; React components consume the resolved hex/text styles.
import { categoryParentId } from "./category_tree";

export interface CategoryColorInput {
  id: string;
  parent_id?: string | null;
  /**
   * Display name. Hashing ignores it. It is read only to recognise the technical
   * cash root, because the API category has no system flag and does not expose `name_key`.
   */
  name?: string | undefined;
}

export interface OklchColor {
  l: number;
  c: number;
  h: number;
}

export interface PaletteSwatch {
  name: string;
  oklch: OklchColor;
  background: string;
  text: "#000000" | "#ffffff";
}

export interface CategoryColor {
  background: string;
  text: "#000000" | "#ffffff";
  oklch: OklchColor;
  depth: number;
  rootId: string | null;
  neutral: boolean;
  hatched: boolean;
}

/**
 * Hook for a later optional per-root user colour override (maps root category id -> CSS hex `#rrggbb` or `#rgb`).
 * UI editing and persistence are deliberately deferred beyond v1.
 */
export type CategoryColorOverrides =
  | ReadonlyMap<string, string>
  | Readonly<Record<string, string>>;

export interface CategoryColorOptions {
  overrides?: CategoryColorOverrides | undefined;
}

export interface CategoryColorResolver {
  readonly entries: ReadonlyMap<string, CategoryColor>;
  get: (categoryId: string | null, options?: { oneOff?: boolean | undefined }) => CategoryColor;
}

export interface SwatchStyle {
  backgroundColor: string;
  backgroundImage?: string;
  borderColor?: string;
}

export interface BadgeStyle {
  backgroundColor: string;
  color: "#000000" | "#ffffff";
  backgroundImage?: string;
  borderColor?: string;
}

const DEPTH_LIGHTNESS_STEP = 0.11;
const DARKEN_THRESHOLD_L = 0.65;
const MIN_LIGHTNESS = 0.18;
const MAX_LIGHTNESS = 0.90;
const MAX_SHADE_DEPTH = 3;
const MAX_WALK_DEPTH = 10_000;
const CHROMA_SCALE_BY_DEPTH = [1, 0.86, 0.70] as const;
// Alternates slightly inside the clamped depth-3 anchor so levels deeper than 3 remain distinct.
const DEEP_LIGHTNESS_OFFSETS = [-0.04, -0.08, -0.025, -0.06] as const;
const DEEP_HUE_OFFSETS = [4, -4, 7, -7] as const;
const SIBLING_LIGHTNESS_OFFSETS = [-0.018, 0.018, -0.009, 0.009, 0] as const;
const MAX_SIBLING_HUE_SHIFT_DEG = 14;
const NEUTRAL_OKLCH: OklchColor = { l: 0.56, c: 0.015, h: 65 };

const CURATED_ROOT_HUES: readonly { name: string; oklch: OklchColor }[] = [
  { name: "terracotta", oklch: { l: 0.55, c: 0.16, h: 32 } },
  { name: "cobalt", oklch: { l: 0.55, c: 0.16, h: 250 } },
  { name: "forest", oklch: { l: 0.55, c: 0.14, h: 145 } },
  { name: "plum", oklch: { l: 0.55, c: 0.15, h: 305 } },
  { name: "amber", oklch: { l: 0.60, c: 0.15, h: 78 } },
  { name: "teal", oklch: { l: 0.55, c: 0.12, h: 192 } },
  { name: "crimson", oklch: { l: 0.55, c: 0.17, h: 14 } },
  { name: "indigo", oklch: { l: 0.54, c: 0.16, h: 274 } },
  { name: "olive", oklch: { l: 0.57, c: 0.13, h: 112 } },
  { name: "magenta", oklch: { l: 0.55, c: 0.16, h: 340 } },
  { name: "cerulean", oklch: { l: 0.57, c: 0.13, h: 222 } },
  { name: "ochre", oklch: { l: 0.57, c: 0.13, h: 54 } },
];

function normalizeHue(deg: number): number {
  const mod = deg % 360;
  return mod < 0 ? mod + 360 : mod;
}

function clampUnit(value: number): number {
  return value < 0 ? 0 : value > 1 ? 1 : value;
}

function srgbChannelToLinear(byte: number): number {
  const normalized = byte / 255;
  return normalized <= 0.04045 ? normalized / 12.92 : Math.pow((normalized + 0.055) / 1.055, 2.4);
}

function linearChannelToSrgbByte(linear: number): number {
  const clamped = clampUnit(linear);
  const encoded = clamped <= 0.0031308 ? 12.92 * clamped : 1.055 * Math.pow(clamped, 1 / 2.4) - 0.055;
  return Math.round(encoded * 255);
}

function oklchToLinearSrgb(l: number, c: number, hDeg: number): readonly [number, number, number] {
  const rad = (hDeg * Math.PI) / 180;
  const a = c * Math.cos(rad);
  const b = c * Math.sin(rad);
  const lCone = l + 0.3963377774 * a + 0.2158037573 * b;
  const mCone = l - 0.1055613458 * a - 0.0638541728 * b;
  const sCone = l - 0.0894841775 * a - 1.2914855480 * b;
  const lCubed = lCone * lCone * lCone;
  const mCubed = mCone * mCone * mCone;
  const sCubed = sCone * sCone * sCone;
  return [
    4.0767416621 * lCubed - 3.3077115913 * mCubed + 0.2309699292 * sCubed,
    -1.2684380046 * lCubed + 2.6097574011 * mCubed - 0.3413193965 * sCubed,
    -0.0041960863 * lCubed - 0.7034186147 * mCubed + 1.7076147010 * sCubed,
  ];
}

function inSrgbGamut(rgb: readonly [number, number, number]): boolean {
  return rgb[0] >= -1e-6 && rgb[0] <= 1 + 1e-6
    && rgb[1] >= -1e-6 && rgb[1] <= 1 + 1e-6
    && rgb[2] >= -1e-6 && rgb[2] <= 1 + 1e-6;
}

function bytesToHex(bytes: readonly [number, number, number]): string {
  const hexByte = (value: number) => Math.max(0, Math.min(255, value)).toString(16).padStart(2, "0");
  return `#${hexByte(bytes[0])}${hexByte(bytes[1])}${hexByte(bytes[2])}`;
}

/** Convert OKLCH coordinates to `#rrggbb`, preserving lightness and hue via binary-search chroma reduction. */
export function oklchToHex(l: number, c: number, h: number): string {
  const safeL = clampUnit(l);
  const safeC = Math.max(0, c);
  const safeH = normalizeHue(h);
  let best = oklchToLinearSrgb(safeL, safeC, safeH);
  if (!inSrgbGamut(best)) {
    let low = 0;
    let high = safeC;
    best = oklchToLinearSrgb(safeL, 0, safeH);
    for (let iter = 0; iter < 24; iter += 1) {
      const mid = (low + high) / 2;
      const candidate = oklchToLinearSrgb(safeL, mid, safeH);
      if (inSrgbGamut(candidate)) {
        low = mid;
        best = candidate;
      } else {
        high = mid;
      }
    }
  }
  return bytesToHex([
    linearChannelToSrgbByte(best[0]),
    linearChannelToSrgbByte(best[1]),
    linearChannelToSrgbByte(best[2]),
  ]);
}

/** Parse `#rgb` or `#rrggbb` into normalized 8-bit RGB channels and canonical lowercase `#rrggbb`. */
export function parseHexColor(value: string): { r: number; g: number; b: number; hex: string } | null {
  const trimmed = value.trim();
  const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(trimmed);
  if (match === null) return null;
  const digits = match[1] ?? "";
  const full = digits.length === 3
    ? `${digits.charAt(0)}${digits.charAt(0)}${digits.charAt(1)}${digits.charAt(1)}${digits.charAt(2)}${digits.charAt(2)}`
    : digits;
  const r = Number.parseInt(full.slice(0, 2), 16);
  const g = Number.parseInt(full.slice(2, 4), 16);
  const b = Number.parseInt(full.slice(4, 6), 16);
  return { r, g, b, hex: `#${full.toLowerCase()}` };
}

/** Convert `#rgb` / `#rrggbb` to perceptual OKLCH coordinates. Returns null for invalid hex strings. */
export function hexToOklch(hex: string): OklchColor | null {
  const parsed = parseHexColor(hex);
  if (parsed === null) return null;
  const r = srgbChannelToLinear(parsed.r);
  const g = srgbChannelToLinear(parsed.g);
  const b = srgbChannelToLinear(parsed.b);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const lOut = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s;
  const aOut = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s;
  const bOut = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s;
  const c = Math.hypot(aOut, bOut);
  const h = c < 1e-6 ? 0 : normalizeHue((Math.atan2(bOut, aOut) * 180) / Math.PI);
  return { l: lOut, c, h };
}

/** WCAG 2.1 relative luminance for `#rgb` / `#rrggbb`. */
export function relativeLuminance(hex: string): number {
  const parsed = parseHexColor(hex);
  if (parsed === null) return 0;
  return 0.2126 * srgbChannelToLinear(parsed.r)
    + 0.7152 * srgbChannelToLinear(parsed.g)
    + 0.0722 * srgbChannelToLinear(parsed.b);
}

/** WCAG 2.1 contrast ratio between two hex colors (in `[1, 21]`). */
export function contrastRatio(hexA: string, hexB: string): number {
  const lumA = relativeLuminance(hexA);
  const lumB = relativeLuminance(hexB);
  const light = lumA > lumB ? lumA : lumB;
  const dark = lumA > lumB ? lumB : lumA;
  return (light + 0.05) / (dark + 0.05);
}

/** Select black (`#000000`) or white (`#ffffff`) text maximizing WCAG contrast (always >= 4.58:1 for valid sRGB). */
export function accessibleBadgeText(backgroundHex: string): "#000000" | "#ffffff" {
  return contrastRatio(backgroundHex, "#ffffff") >= contrastRatio(backgroundHex, "#000000")
    ? "#ffffff"
    : "#000000";
}

export const CATEGORY_PALETTE: readonly PaletteSwatch[] = CURATED_ROOT_HUES.map(({ name, oklch }) => {
  const background = oklchToHex(oklch.l, oklch.c, oklch.h);
  return { name, oklch, background, text: accessibleBadgeText(background) };
});

const NEUTRAL_BACKGROUND = oklchToHex(NEUTRAL_OKLCH.l, NEUTRAL_OKLCH.c, NEUTRAL_OKLCH.h);

export const NEUTRAL_CATEGORY_COLOR: CategoryColor = {
  background: NEUTRAL_BACKGROUND,
  text: accessibleBadgeText(NEUTRAL_BACKGROUND),
  oklch: NEUTRAL_OKLCH,
  depth: 0,
  rootId: null,
  neutral: true,
  hatched: false,
};

export const OTHER_CATEGORY_COLOR: CategoryColor = {
  ...NEUTRAL_CATEGORY_COLOR,
  hatched: true,
};

/**
 * Server `budget_categories.name_key` is `" ".join(_text(name).split()).casefold()`
 * (`analytics_store._text` NFC-strips, then `create_category` collapses whitespace and casefolds).
 * `BudgetAnalyticsCategory` has no system flag and the wire type does not include `name_key`,
 * so this root cannot be pinned by a production UUID. Python `str.casefold()` of
 * "Gotówka (bankomat)" keeps ó, giving exactly `gotówka (bankomat)`. `String#toLowerCase()`
 * matches casefold for that Polish label.
 */
export const CASH_ATM_NAME_KEY = "gotówka (bankomat)";

/** Slate blue-grey, fixed so this root does not hash onto the red/carmine family or the uncategorised grey. */
const CASH_ATM_OKLCH: OklchColor = { l: 0.46, c: 0.045, h: 250 };
export const CASH_ATM_COLOR = oklchToHex(CASH_ATM_OKLCH.l, CASH_ATM_OKLCH.c, CASH_ATM_OKLCH.h);

/** Same key the server stores: NFC, collapsed whitespace, casefold. */
export function categoryNameKey(name: string): string {
  return name.normalize("NFC").split(/\s+/u).filter((part) => part !== "").join(" ").toLowerCase();
}

function isColorRoot(item: CategoryColorInput, ids: ReadonlySet<string>): boolean {
  const parent = categoryParentId(item);
  return parent === null || !ids.has(parent) || parent === item.id;
}

/** Fixed override for the cash root only. Descendants shade from it through the existing override hook. */
function technicalCashOverrides(categories: readonly CategoryColorInput[]): CategoryColorOverrides | undefined {
  const ids = new Set(categories.map((item) => item.id));
  const overrides: Record<string, string> = {};
  for (const item of categories) {
    if (item.name === undefined || !isColorRoot(item, ids)) continue;
    if (categoryNameKey(item.name) !== CASH_ATM_NAME_KEY) continue;
    overrides[item.id] = CASH_ATM_COLOR;
  }
  return Object.keys(overrides).length === 0 ? undefined : overrides;
}

function fnv1a32(value: string): number {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

function compareIds(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function isReadonlyMap<K, V>(value: unknown): value is ReadonlyMap<K, V> {
  return typeof value === "object" && value !== null && typeof (value as { get?: unknown }).get === "function";
}

function lookupOverride(overrides: CategoryColorOverrides | undefined, id: string): string | undefined {
  if (overrides === undefined) return undefined;
  if (isReadonlyMap<string, string>(overrides)) return overrides.get(id);
  return Object.hasOwn(overrides, id) ? overrides[id] : undefined;
}

function parseOverrideColor(raw: string | undefined): { background: string; oklch: OklchColor } | null {
  if (raw === undefined) return null;
  const parsed = parseHexColor(raw);
  if (parsed === null) return null;
  const oklch = hexToOklch(parsed.hex);
  if (oklch === null) return null;
  return { background: parsed.hex, oklch };
}

function siblingOffsets(index: number, count: number): { dH: number; dL: number } {
  if (count <= 1) return { dH: 0, dL: 0 };
  const t = (index / (count - 1)) * 2 - 1;
  const dH = t * MAX_SIBLING_HUE_SHIFT_DEG;
  const baseLight = SIBLING_LIGHTNESS_OFFSETS[index % SIBLING_LIGHTNESS_OFFSETS.length] ?? 0;
  const tier = Math.floor(index / SIBLING_LIGHTNESS_OFFSETS.length);
  const extraLight = tier === 0 ? 0 : (tier % 2 === 1 ? -0.004 : 0.004) * Math.ceil(tier / 2);
  return { dH, dL: baseLight + extraLight };
}

function clampedLightness(rawL: number): number {
  return Math.max(MIN_LIGHTNESS, Math.min(MAX_LIGHTNESS, rawL));
}

function shadeForDescendant(
  rootOklch: OklchColor,
  depth: number,
  branchHueBias: number,
  siblingIndex: number,
  siblingCount: number,
): { oklch: OklchColor; background: string; hueOffset: number } {
  const direction = rootOklch.l <= DARKEN_THRESHOLD_L ? 1 : -1;
  const { dH, dL } = siblingOffsets(siblingIndex, siblingCount);
  let baseL: number;
  let deepHue = 0;
  if (depth <= MAX_SHADE_DEPTH) {
    baseL = clampedLightness(rootOklch.l + direction * DEPTH_LIGHTNESS_STEP * depth);
  } else {
    const anchorL = clampedLightness(rootOklch.l + direction * DEPTH_LIGHTNESS_STEP * MAX_SHADE_DEPTH);
    const wrapIndex = (depth - MAX_SHADE_DEPTH - 1) % DEEP_LIGHTNESS_OFFSETS.length;
    const wrapDelta = DEEP_LIGHTNESS_OFFSETS[wrapIndex] ?? -0.04;
    baseL = anchorL + direction * wrapDelta;
    deepHue = DEEP_HUE_OFFSETS[wrapIndex] ?? 0;
  }
  const l = clampedLightness(baseL + dL);
  const chromaScale = CHROMA_SCALE_BY_DEPTH[depth] ?? CHROMA_SCALE_BY_DEPTH[CHROMA_SCALE_BY_DEPTH.length - 1] ?? 0.70;
  const c = rootOklch.c * chromaScale;
  const hueOffset = branchHueBias * 0.6 + dH;
  const h = normalizeHue(rootOklch.h + hueOffset + deepHue);
  const oklch: OklchColor = { l, c, h };
  return { oklch, background: oklchToHex(l, c, h), hueOffset };
}

function rootBaseColor(
  id: string,
  overrides?: CategoryColorOverrides,
  technical?: CategoryColorOverrides,
): { oklch: OklchColor; background: string } {
  // An explicit caller override wins. An invalid one falls through to the technical cash colour, then the hash.
  const override = parseOverrideColor(lookupOverride(overrides, id))
    ?? parseOverrideColor(lookupOverride(technical, id));
  if (override !== null) return override;
  const slot = fnv1a32(id) % CATEGORY_PALETTE.length;
  const swatch = CATEGORY_PALETTE[slot] ?? CATEGORY_PALETTE[0];
  if (swatch === undefined) return { oklch: NEUTRAL_OKLCH, background: NEUTRAL_BACKGROUND };
  return { oklch: swatch.oklch, background: swatch.background };
}

function fallbackRootColor(id: string, overrides?: CategoryColorOverrides): CategoryColor {
  const base = rootBaseColor(id, overrides);
  return {
    background: base.background,
    text: accessibleBadgeText(base.background),
    oklch: base.oklch,
    depth: 0,
    rootId: id,
    neutral: false,
    hatched: false,
  };
}

interface WalkFrame {
  id: string;
  depth: number;
  hueBias: number;
  siblingIndex: number;
  siblingCount: number;
}

/**
 * Assign deterministic colours to a category tree:
 * - Each root independently takes a curated hue via `fnv1a(root.id) % paletteSize` (stable across root additions/removals).
 * - Descendants shade their parent's hue monotonically through level 3 and wrap alternation deeper down.
 * - The technical cash root (`CASH_ATM_NAME_KEY`) takes a fixed slate. That override is by name_key, not by which roots are on screen.
 * - Valid optional overrides replace only their own root's base colour and propagate shades to its descendants.
 *   An explicit override wins over the cash default.
 */
export function buildCategoryColorMap(
  categories: readonly CategoryColorInput[],
  options?: CategoryColorOptions,
): ReadonlyMap<string, CategoryColor> {
  const byId = new Map<string, CategoryColorInput>();
  for (const item of categories) {
    if (!byId.has(item.id)) byId.set(item.id, item);
  }

  const children = new Map<string, string[]>();
  const roots: string[] = [];
  for (const item of byId.values()) {
    const parent = categoryParentId(item);
    if (parent === null || !byId.has(parent) || parent === item.id) {
      roots.push(item.id);
    } else {
      const list = children.get(parent) ?? [];
      list.push(item.id);
      children.set(parent, list);
    }
  }
  roots.sort(compareIds);
  for (const list of children.values()) list.sort(compareIds);

  const resolved = new Map<string, CategoryColor>();
  const technical = technicalCashOverrides([...byId.values()]);

  const walkComponent = (seedId: string) => {
    if (resolved.has(seedId)) return;
    const { oklch: rootOklch, background: rootBackground } = rootBaseColor(seedId, options?.overrides, technical);
    const stack: WalkFrame[] = [{ id: seedId, depth: 0, hueBias: 0, siblingIndex: 0, siblingCount: 1 }];
    while (stack.length > 0) {
      const frame = stack.pop();
      if (frame === undefined || resolved.has(frame.id)) continue;
      let nextHueBias = frame.hueBias;
      if (frame.depth === 0) {
        resolved.set(frame.id, {
          background: rootBackground,
          text: accessibleBadgeText(rootBackground),
          oklch: rootOklch,
          depth: 0,
          rootId: seedId,
          neutral: false,
          hatched: false,
        });
      } else {
        const shaded = shadeForDescendant(
          rootOklch,
          frame.depth,
          frame.hueBias,
          frame.siblingIndex,
          frame.siblingCount,
        );
        nextHueBias = shaded.hueOffset;
        resolved.set(frame.id, {
          background: shaded.background,
          text: accessibleBadgeText(shaded.background),
          oklch: shaded.oklch,
          depth: frame.depth,
          rootId: seedId,
          neutral: false,
          hatched: false,
        });
      }
      if (frame.depth >= MAX_WALK_DEPTH) continue;
      const kids = children.get(frame.id) ?? [];
      for (let idx = kids.length - 1; idx >= 0; idx -= 1) {
        const childId = kids[idx];
        if (childId !== undefined && !resolved.has(childId)) {
          stack.push({
            id: childId,
            depth: frame.depth + 1,
            hueBias: nextHueBias,
            siblingIndex: idx,
            siblingCount: kids.length,
          });
        }
      }
    }
  };

  for (const rootId of roots) walkComponent(rootId);

  // Cycle and depth-cap fallback: remaining nodes seed iteratively in sorted ID order.
  const remaining = [...byId.keys()].filter((id) => !resolved.has(id)).sort(compareIds);
  for (const id of remaining) walkComponent(id);

  return resolved;
}

/** Resolve one category id (or `null` for uncategorised) from a colour map, with optional one-off hatching. */
export function resolveCategoryColor(
  map: ReadonlyMap<string, CategoryColor>,
  categoryId: string | null,
  options?: { oneOff?: boolean | undefined; overrides?: CategoryColorOverrides | undefined },
): CategoryColor {
  const base = categoryId === null
    ? NEUTRAL_CATEGORY_COLOR
    : (map.get(categoryId) ?? fallbackRootColor(categoryId, options?.overrides));
  return options?.oneOff === true ? { ...base, hatched: true } : base;
}

/** Create a category colour resolver object bound to a category list and optional root overrides. */
export function buildCategoryColors(
  categories: readonly CategoryColorInput[],
  options?: CategoryColorOptions,
): CategoryColorResolver {
  const entries = buildCategoryColorMap(categories, options);
  return {
    entries,
    get: (categoryId, getOptions) =>
      resolveCategoryColor(entries, categoryId, {
        oneOff: getOptions?.oneOff,
        overrides: options?.overrides,
      }),
  };
}

/** Resolve a one-off labelled entry's colour: its category's colour hatched, or neutral hatched when uncategorised. */
export function oneOffCategoryColor(
  colors: CategoryColorResolver | ReadonlyMap<string, CategoryColor>,
  categoryId: string | null,
): CategoryColor {
  if (isReadonlyMap<string, CategoryColor>(colors)) {
    return resolveCategoryColor(colors, categoryId, { oneOff: true });
  }
  return colors.get(categoryId, { oneOff: true });
}

export function hatchedSwatchGradient(backgroundHex: string): string {
  return `repeating-linear-gradient(45deg, ${backgroundHex}, ${backgroundHex} 2px, var(--surface) 2px, var(--surface) 4px)`;
}

/** Hatched badge fill that preserves WCAG AA contrast on both stripes against the chosen text colour. */
export function hatchedBadgeGradient(color: Pick<CategoryColor, "background" | "text" | "oklch">): string {
  const deltaL = color.text === "#ffffff" ? -0.045 : 0.045;
  const candidateL = clampUnit(color.oklch.l + deltaL);
  const candidate = oklchToHex(candidateL, color.oklch.c, color.oklch.h);
  const baseContrast = contrastRatio(color.background, color.text);
  const stripe = contrastRatio(candidate, color.text) >= Math.min(4.5, baseContrast) ? candidate : color.background;
  return `repeating-linear-gradient(45deg, ${color.background}, ${color.background} 5px, ${stripe} 5px, ${stripe} 8px)`;
}

export function categorySwatchStyle(color: Pick<CategoryColor, "background" | "hatched">): SwatchStyle {
  if (!color.hatched) return { backgroundColor: color.background };
  return {
    backgroundColor: color.background,
    backgroundImage: hatchedSwatchGradient(color.background),
    borderColor: color.background,
  };
}

export function categoryBadgeStyle(color: Pick<CategoryColor, "background" | "text" | "oklch" | "hatched">): BadgeStyle {
  if (!color.hatched) {
    return { backgroundColor: color.background, color: color.text };
  }
  return {
    backgroundColor: color.background,
    color: color.text,
    backgroundImage: hatchedBadgeGradient(color),
  };
}
