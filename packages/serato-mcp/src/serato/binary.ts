/**
 * Serato's tag-length-value container format, used by `database V2`,
 * `*.crate` and `*.scrate` files.
 *
 * Each field is a 4-byte ASCII tag, a 4-byte big-endian length, then the
 * payload. The first letter of the tag gives the payload type:
 *   o  nested fields        t  UTF-16BE text      p  UTF-16BE path
 *   u  uint32               s  uint16             b  single byte
 *   r  nested fields (smart-crate rules)
 * Anything else is kept as raw bytes.
 *
 * Every field keeps its original bytes, so a file that is decoded and
 * re-encoded without changes comes out byte-identical.
 */

export interface Field {
  tag: string;
  raw: Buffer;
  children?: Field[];
}

const NESTED = new Set(["o", "r"]);

export function isNestedTag(tag: string): boolean {
  return NESTED.has(tag[0]);
}

export function decode(buf: Buffer): Field[] {
  const fields: Field[] = [];
  let pos = 0;
  while (pos + 8 <= buf.length) {
    const tag = buf.toString("latin1", pos, pos + 4);
    const len = buf.readUInt32BE(pos + 4);
    const start = pos + 8;
    const end = start + len;
    if (end > buf.length) throw new Error(`Corrupt Serato data: field ${tag} at ${pos} overruns buffer`);
    const raw = buf.subarray(start, end);
    const field: Field = { tag, raw };
    if (isNestedTag(tag)) field.children = decode(raw);
    fields.push(field);
    pos = end;
  }
  return fields;
}

export function encode(fields: Field[]): Buffer {
  const parts: Buffer[] = [];
  for (const f of fields) {
    const payload = f.children ? encode(f.children) : f.raw;
    const head = Buffer.alloc(8);
    head.write(f.tag, 0, 4, "latin1");
    head.writeUInt32BE(payload.length, 4);
    parts.push(head, payload);
  }
  return Buffer.concat(parts);
}

// ---- UTF-16BE ------------------------------------------------------------

export function decodeUtf16BE(raw: Buffer): string {
  const swapped = Buffer.from(raw);
  swapped.swap16();
  return swapped.toString("utf16le");
}

export function encodeUtf16BE(text: string): Buffer {
  const b = Buffer.from(text, "utf16le");
  b.swap16();
  return b;
}

// ---- typed accessors -------------------------------------------------------

export type FieldValue = string | number | boolean | Buffer;

const TEXT_TAGS = new Set(["vrsn"]);

export function readValue(f: Field): FieldValue {
  if (TEXT_TAGS.has(f.tag)) return decodeUtf16BE(f.raw);
  switch (f.tag[0]) {
    case "t":
    case "p":
      return decodeUtf16BE(f.raw);
    case "u":
      return f.raw.length === 4 ? f.raw.readUInt32BE(0) : f.raw;
    case "s":
      return f.raw.length === 2 ? f.raw.readUInt16BE(0) : f.raw;
    case "b":
      return f.raw.length === 1 ? f.raw[0] !== 0 : f.raw;
    default:
      return f.raw;
  }
}

export function makeField(tag: string, value: FieldValue): Field {
  if (Buffer.isBuffer(value)) return { tag, raw: value };
  if (TEXT_TAGS.has(tag)) return { tag, raw: encodeUtf16BE(String(value)) };
  switch (tag[0]) {
    case "t":
    case "p":
      return { tag, raw: encodeUtf16BE(String(value)) };
    case "u": {
      const b = Buffer.alloc(4);
      b.writeUInt32BE(Number(value) >>> 0, 0);
      return { tag, raw: b };
    }
    case "s": {
      const b = Buffer.alloc(2);
      b.writeUInt16BE(Number(value) & 0xffff, 0);
      return { tag, raw: b };
    }
    case "b":
      return { tag, raw: Buffer.from([value ? 1 : 0]) };
    default:
      throw new Error(`Cannot encode value for tag ${tag}`);
  }
}

export function getChild(parent: Field, tag: string): Field | undefined {
  return parent.children?.find((c) => c.tag === tag);
}

/** Set (or insert) a child value, keeping its position when it already exists. */
export function setChild(parent: Field, tag: string, value: FieldValue): void {
  if (!parent.children) throw new Error(`${parent.tag} is not a nested field`);
  const next = makeField(tag, value);
  const idx = parent.children.findIndex((c) => c.tag === tag);
  if (idx >= 0) parent.children[idx] = next;
  else parent.children.push(next);
}

export function removeChild(parent: Field, tag: string): void {
  if (!parent.children) return;
  parent.children = parent.children.filter((c) => c.tag !== tag);
}
