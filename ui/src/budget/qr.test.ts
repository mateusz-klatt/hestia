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
  for (let root = 0; root < 10; root += 1) {
    let sum = 0;
    const x = exp[root] ?? 1;
    for (const byte of block) sum = mul(sum, x) ^ byte;
    out.push(sum);
  }
  return out;
}

describe("pairing QR", () => {
  it("uses the version-2 L mask-0 format bits", () => {
    expect(formatBits(1, 0)).toBe(0x77c4);
  });

  it("builds a 25 by 25 code whose ECC syndromes are zero", () => {
    const payload = "hestia-pair:01234567";
    const block = encodeBlock(payload);
    expect(block).not.toBeNull();
    expect(syndromes(block ?? new Uint8Array())).toEqual(Array<number>(10).fill(0));
    const matrix = qrMatrix(payload);
    expect(matrix).not.toBeNull();
    expect(matrix?.length).toBe(25);
    expect(matrix?.[0]?.[0]).toBe(true);
    expect(matrix?.[1]?.[1]).toBe(false);
    expect(matrix?.[3]?.[3]).toBe(true);
    expect(matrix?.[17]?.[8]).toBe(true);
    // Rows packed left to right from Project Nayuki's reference encoder, version 2/L, byte mode, mask 0.
    expect(matrix?.map((row) => row.reduce((packed, on) => (packed << 1) | Number(on), 0))).toEqual([
      0x1fc437f, 0x1048e41, 0x175205d, 0x174935d, 0x174935d,
      0x1047741, 0x1fd557f, 0x0012800, 0x1df1cc4, 0x1a3bcc9,
      0x057793b, 0x0b2dc20, 0x0a464cb, 0x0a9e043, 0x16fc857,
      0x0f3a2e2, 0x14fd1f0, 0x001391f, 0x1fdf75f, 0x1055f12,
      0x17525f3, 0x1746698, 0x17509bd, 0x10561ca, 0x1fd9123,
    ]);
  });

  it("returns null when the payload is longer than a version-2 byte code", () => {
    expect(qrMatrix("x".repeat(33))).toBeNull();
    expect(encodeBlock("x".repeat(33))).toBeNull();
  });
});
