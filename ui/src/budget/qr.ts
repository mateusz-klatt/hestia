/** Version 2, ECC L, byte mode, mask 0. Enough for `hestia-pair:` plus an 8-character code. */

const SIZE = 25;
const DATA_CODEWORDS = 34;
const ECC_CODEWORDS = 10;
const MAX_BYTES = 32;

const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);

function buildField(): void {
  let value = 1;
  for (let exponent = 0; exponent < 255; exponent += 1) {
    EXP[exponent] = value;
    LOG[value] = exponent;
    value <<= 1;
    if (value >= 256) value ^= 0x11d;
  }
  for (let exponent = 255; exponent < 512; exponent += 1) {
    const lower = EXP[exponent - 255];
    if (lower !== undefined) EXP[exponent] = lower;
  }
}

buildField();

function fieldAt(table: Uint8Array, index: number): number {
  const value = table[index];
  if (value === undefined) throw new Error("qr field");
  return value;
}

function mul(left: number, right: number): number {
  if (left === 0 || right === 0) return 0;
  return fieldAt(EXP, fieldAt(LOG, left) + fieldAt(LOG, right));
}

function generator(degree: number): number[] {
  let poly = [1];
  for (let root = 0; root < degree; root += 1) {
    const factor = fieldAt(EXP, root);
    const next = Array<number>(poly.length + 1).fill(0);
    for (let index = 0; index < poly.length; index += 1) {
      const coeff = poly[index] ?? 0;
      next[index] = (next[index] ?? 0) ^ coeff;
      next[index + 1] = (next[index + 1] ?? 0) ^ mul(coeff, factor);
    }
    poly = next;
  }
  return poly;
}

export function formatBits(eccLevel: number, mask: number): number {
  const data = (eccLevel << 3) | mask;
  let remainder = data << 10;
  for (let bit = 14; bit >= 10; bit -= 1) {
    if (((remainder >>> bit) & 1) !== 0) remainder ^= 0x537 << (bit - 10);
  }
  return ((data << 10) | remainder) ^ 0x5412;
}

function dataCodewords(payload: Uint8Array): number[] | null {
  if (payload.length > MAX_BYTES) return null;
  const bits: number[] = [];
  const push = (value: number, width: number) => {
    for (let shift = width - 1; shift >= 0; shift -= 1) bits.push((value >>> shift) & 1);
  };
  push(0b0100, 4);
  push(payload.length, 8);
  for (const byte of payload) push(byte, 8);
  const capacity = DATA_CODEWORDS * 8;
  for (let pad = 0; pad < 4 && bits.length < capacity; pad += 1) bits.push(0);
  while (bits.length % 8 !== 0) bits.push(0);
  const bytes: number[] = [];
  for (let index = 0; index < bits.length; index += 8) {
    let byte = 0;
    for (let shift = 0; shift < 8; shift += 1) byte = (byte << 1) | (bits[index + shift] ?? 0);
    bytes.push(byte);
  }
  for (let pad = 0; bytes.length < DATA_CODEWORDS; pad += 1) bytes.push(pad % 2 === 0 ? 0xec : 0x11);
  return bytes;
}

function eccCodewords(data: readonly number[]): number[] {
  const poly = generator(ECC_CODEWORDS);
  const block = [...data, ...Array<number>(ECC_CODEWORDS).fill(0)];
  for (let index = 0; index < data.length; index += 1) {
    const factor = block[index] ?? 0;
    if (factor === 0) continue;
    for (let offset = 0; offset < poly.length; offset += 1) {
      block[index + offset] = (block[index + offset] ?? 0) ^ mul(poly[offset] ?? 0, factor);
    }
  }
  return block.slice(data.length);
}

export function encodeBlock(payload: string): Uint8Array | null {
  const bytes = new TextEncoder().encode(payload);
  const data = dataCodewords(bytes);
  if (data === null) return null;
  return Uint8Array.from([...data, ...eccCodewords(data)]);
}

function maskBit(x: number, y: number): boolean {
  return (x + y) % 2 === 0;
}

export function qrMatrix(payload: string): boolean[][] | null {
  const block = encodeBlock(payload);
  if (block === null) return null;
  const dark = Array.from({ length: SIZE }, () => Array<boolean>(SIZE).fill(false));
  const reserved = Array.from({ length: SIZE }, () => Array<boolean>(SIZE).fill(false));
  const set = (x: number, y: number, on: boolean) => {
    const row = dark[y];
    const hold = reserved[y];
    if (row === undefined || hold === undefined) return;
    row[x] = on;
    hold[x] = true;
  };
  const finder = (ox: number, oy: number) => {
    for (let y = -1; y <= 7; y += 1) {
      for (let x = -1; x <= 7; x += 1) {
        const xx = ox + x;
        const yy = oy + y;
        if (xx < 0 || yy < 0 || xx >= SIZE || yy >= SIZE) continue;
        const onFinder = x >= 0 && x <= 6 && y >= 0 && y <= 6;
        const border = x === 0 || y === 0 || x === 6 || y === 6;
        const core = x >= 2 && x <= 4 && y >= 2 && y <= 4;
        set(xx, yy, onFinder && (border || core));
      }
    }
  };
  finder(0, 0);
  finder(SIZE - 7, 0);
  finder(0, SIZE - 7);
  for (let index = 0; index < SIZE; index += 1) {
    if (!reserved[6]?.[index]) set(index, 6, index % 2 === 0);
    if (!reserved[index]?.[6]) set(6, index, index % 2 === 0);
  }
  for (let y = -2; y <= 2; y += 1) {
    for (let x = -2; x <= 2; x += 1) set(18 + x, 18 + y, Math.max(Math.abs(x), Math.abs(y)) !== 1);
  }
  set(8, SIZE - 8, true);
  const format = formatBits(1, 0);
  const bit = (index: number) => ((format >>> (14 - index)) & 1) === 1;
  for (let index = 0; index <= 5; index += 1) set(8, index, bit(index));
  set(8, 7, bit(6));
  set(8, 8, bit(7));
  set(7, 8, bit(8));
  for (let index = 9; index < 15; index += 1) set(14 - index, 8, bit(index));
  for (let index = 0; index < 8; index += 1) set(SIZE - 1 - index, 8, bit(index));
  for (let index = 8; index < 15; index += 1) set(8, SIZE - 15 + index, bit(index));
  let cursor = 0;
  for (let right = SIZE - 1; right >= 1; right -= 2) {
    if (right === 6) right -= 1;
    for (let vert = 0; vert < SIZE; vert += 1) {
      for (let column = 0; column < 2; column += 1) {
        const x = right - column;
        const upward = ((right + 1) & 2) === 0;
        const y = upward ? SIZE - 1 - vert : vert;
        if (reserved[y]?.[x]) continue;
        const source = block[cursor >>> 3] ?? 0;
        const on = ((source >>> (7 - (cursor & 7))) & 1) !== 0;
        cursor += 1;
        const row = dark[y];
        if (row !== undefined) row[x] = on !== maskBit(x, y);
      }
    }
  }
  return dark;
}
