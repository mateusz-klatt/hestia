/**
 * Blind position scale: the mapping between the wire `cover` value (0–99, what the device speaks) and the
 * displayed "openness" percent the user sees on the slider.
 *
 * It is deliberately NON-LINEAR, to match how venetian blinds actually open: the slats stack/curl as the
 * blind raises, so perceived openness LAGS the wire value (commanding ~64 looks about half-open). And the
 * bottom is a DEAD-ZONE — wire 0 is the only fully-closed/opaque state, while any small lift (≈ wire 10)
 * already lets light through and all of 1–9 look the same. So:
 *   • 0 % ⟺ wire 0  — fully closed, opaque (the only "shut" state).
 *   • the first step above 0 jumps to wire 10 — lowered but see-through; wire 1–9 are never commanded and
 *     a reported 1–9 (e.g. an external move) reads as 1 %.
 *   • above that, a power curve (exponent BLIND_EXP) so the slider reads like real openness:
 *     wire 50 → 33 %, wire 64 → 50 %, wire 99 → 100 % (fitted to the operator's eyeball anchors).
 *
 * BLIND_EXP is the single tuning knob — raise it if the mid-slider still looks too closed, lower it if too
 * open. Both directions are clamped + monotonic (non-decreasing), so the slider can never invert; because
 * 100 display steps map onto ~90 wire positions, adjacent percents can share a wire value (harmless).
 */
const BLIND_VISIBLE_WIRE = 10; // smallest wire that is "open a crack" (1–9 are indistinguishable from it)
const BLIND_OPEN_WIRE = 99; // fully open
const BLIND_SPAN = BLIND_OPEN_WIRE - BLIND_VISIBLE_WIRE; // 89
const BLIND_EXP = 1.4; // perceived-openness curve; >1 because openness lags the wire value

/** Wire 0–99 cover value → displayed openness 0–100 %. */
export function coverPercent(wire: number): number {
  const w = Math.min(BLIND_OPEN_WIRE, Math.max(0, wire));
  if (w <= 0) return 0; // fully closed, opaque
  const t = Math.min(1, Math.max(0, (w - BLIND_VISIBLE_WIRE) / BLIND_SPAN)); // 1–9 → 0 → the 1 % floor
  return Math.round(1 + 99 * t ** BLIND_EXP);
}

/** Displayed openness 0–100 % → wire 0–99 cover value. Inverts {@link coverPercent} to within a display
 *  point for a normal position; a round-trip through the dead-zone (wire 1–9) collapses back to wire 10. */
export function coverValue(percent: number): number {
  const p = Math.min(100, Math.max(0, percent));
  if (p <= 0) return 0; // fully closed
  if (p <= 1) return BLIND_VISIBLE_WIRE; // first step above closed → the see-through crack
  return Math.round(BLIND_VISIBLE_WIRE + BLIND_SPAN * ((p - 1) / 99) ** (1 / BLIND_EXP));
}
