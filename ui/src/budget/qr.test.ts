import { describe, expect, it } from "vitest";
import { encodeBlock, formatBits, qrMatrix } from "./qr";

function syndromes(block: Uint8Array): number[] {
  const exp = new Uint8Array(512);
  const log = new Uint8Array(256);
  let value = 1;
  for (let exponent = 0; exponent < 255; exponent += 1) {
    exp[exponent] = value;
    log[value] = exponent;
    value <<= 1;
    if (value >= 256) value ^= 0x11d;
  }
  for (let exponent = 255; exponent < 512; exponent += 1) exp[exponent] = exp[exponent - 255] ?? 0;
  const mul = (left: number, right: number) => {
    if (left === 0 || right === 0) return 0;
    return exp[(log[left] ?? 0) + (log[right] ?? 0)] ?? 0;
  };
  const out: number[] = [];
  for (let root = 0; root < 20; root += 1) {
    let sum = 0;
    const x = exp[root] ?? 1;
    for (const byte of block) sum = mul(sum, x) ^ byte;
    out.push(sum);
  }
  return out;
}

describe("version 4 pairing and login QR", () => {
  it("uses the L mask-0 format bits", () => {
    expect(formatBits(1, 0)).toBe(0x77c4);
  });

  it("builds a 33 by 33 code whose ECC syndromes are zero", () => {
    const payload = "hestia-login:https://hestia.example#AAECAwQFBgcICQoLDA0";
    const block = encodeBlock(payload);
    expect(block?.length).toBe(100);
    expect(syndromes(block ?? new Uint8Array())).toEqual(Array<number>(20).fill(0));
    const matrix = qrMatrix(payload);
    expect(matrix).not.toBeNull();
    expect(matrix?.length).toBe(33);
    expect(matrix?.[0]?.[0]).toBe(true);
    expect(matrix?.[1]?.[1]).toBe(false);
    expect(matrix?.[3]?.[3]).toBe(true);
    // Project Nayuki reference layout, version 4/L, byte mode, mask 0. Strings retain all 33 columns.
    expect(matrix?.map((row) => row.map((on) => on ? "1" : "0").join(""))).toEqual([
      "111111100100110010100111101111111", "100000100011101110111011101000001",
      "101110101110111010101010101011101", "101110100111011101111011001011101",
      "101110100010001000100011001011101", "100000100100010000011111101000001",
      "111111101010101010101010101111111", "000000001110111011001111000000000",
      "111011111011001101000101111000100", "010101000011001101001010101001111",
      "010011101100010001000010111011111", "100000001001000101110101110000010",
      "010100100000100011001100011100011", "011010000101110111011100101001001",
      "010100111101101111100010001011111", "000010000110111011100110010111001",
      "000001101011001110001100011000001", "111011011001001100011000101000101",
      "100110110100010010100000001001111", "010010001011000101111111101110000",
      "010010100100100000001100101100011", "010110000111110001011110101100111",
      "100011111001101001100010100000111", "010110011110111101110101011010010",
      "100011110001001010001101111110000", "000000001001001111111001100010111",
      "111111101000010010100001101011011", "100000101101000111110100100010001",
      "101110101000100011001100111110011", "101110100111110110011100110110001",
      "101110101101101011000001000111001", "100000101100111111100111101000010",
      "111111101101001110001110110000011",
    ]);
  });

  it("keeps legacy pairing and enforces the 78-byte version-4 limit", () => {
    expect(qrMatrix("hestia-pair:01234567")?.length).toBe(33);
    expect(qrMatrix("x".repeat(78))).not.toBeNull();
    expect(qrMatrix("x".repeat(79))).toBeNull();
    expect(encodeBlock("x".repeat(79))).toBeNull();
  });
});
