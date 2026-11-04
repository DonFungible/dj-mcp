/**
 * Read model over one or more `_Serato_` folders: tracks from `database V2`,
 * crates from `Subcrates/`, smart crates and play history.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import { decode, decodeUtf16BE, type Field, getChild, readValue } from "./binary.js";
import { camelotString, keyName, parseKey, type Camelot } from "../mix/keys.js";
import { hasSqliteLibrary, loadSqlite, loadSqliteSessions, splitArtistTitle, sqliteSignature } from "./sqlite.js";

export interface SeratoRoot {
  /** Absolute path of the `_Serato_` folder. */
  dir: string;
  /** Paths stored in this root's files are relative to this directory. */
  volumeRoot: string;
}

export interface Track {
  id?: number; // Serato 4 asset id
  path: string; // absolute path, or streaming:// URI
  source?: string; // local | itunes | streaming:spotify ...
  storedPath: string; // as written in Serato's files
  root: string; // `_Serato_` dir it belongs to
  fileType?: string;
  title?: string;
  artist?: string;
  album?: string;
  genre?: string;
  comment?: string;
  grouping?: string;
  remixer?: string;
  composer?: string;
  label?: string;
  year?: string;
  bpm?: number;
  key?: string; // as stored by Serato
  camelot?: string;
  keyName?: string;
  energy?: number; // 1..10 from Mixed In Key comment, when present
  durationSec?: number;
  bitrate?: string;
  sampleRate?: string;
  sizeBytes?: number;
  color?: string; // #rrggbb (Serato track colour)
  addedAt?: string; // ISO
  rating?: number; // 0-5
  playCount?: number;
  played: boolean;
  missing: boolean; // file not found on disk right now
  corrupt: boolean;
  beatgridLocked: boolean;
  crates: string[];
}

export interface Crate {
  id?: number;
  name: string; // display path, nested with " / "
  kind?: "crate" | "smart crate" | "playlist" | "playlist folder" | "streaming folder" | "streaming playlist";
  space?: string; // Serato Library, iTunes, Spotify...
  rules?: unknown; // smart crate rules
  /** Written to the legacy crate files but not yet imported by Serato 4. */
  pendingImport?: boolean;
  fileName: string; // e.g. "Parent%%Child.crate"
  file: string;
  root: string;
  smart: boolean;
  trackPaths: string[]; // absolute
  columns: string[];
}

export interface HistoryEntry {
  session: string;
  path?: string;
  title?: string;
  artist?: string;
  bpm?: string;
  key?: string;
  deck?: number;
  startTime?: string;
  endTime?: string;
  playTimeSec?: number;
  played?: boolean;
  device?: string;
}

export interface Session {
  id?: number;
  file: string;
  name: string;
  startedAt?: string;
  endedAt?: string;
  notes?: string;
  modifiedAt: string;
  entries: HistoryEntry[];
}

// ---- roots -----------------------------------------------------------------

export function discoverRoots(): SeratoRoot[] {
  const roots: SeratoRoot[] = [];
  const add = (dir: string, volumeRoot: string) => {
    if (existsSync(join(dir, "database V2")) || existsSync(join(dir, "Subcrates"))) {
      if (!roots.some((r) => r.dir === dir)) roots.push({ dir, volumeRoot });
    }
  };
  const env = process.env.SERATO_ROOTS;
  if (env) {
    for (const entry of env.split(":").filter(Boolean)) {
      // "dir" or "dir=volumeRoot"
      const [dir, vol] = entry.split("=");
      add(dir, vol ?? "/");
    }
    return roots;
  }
  add(join(homedir(), "Music", "_Serato_"), "/");
  if (existsSync("/Volumes")) {
    for (const v of readdirSync("/Volumes")) {
      const vol = join("/Volumes", v);
      try {
        if (statSync(vol).isDirectory()) add(join(vol, "_Serato_"), vol + "/");
      } catch {
        /* unreadable volume */
      }
    }
  }
  return roots;
}

export function toAbsolute(root: SeratoRoot, stored: string): string {
  if (stored.startsWith("/")) return stored;
  return join(root.volumeRoot, stored);
}

export function toStored(root: SeratoRoot, absolute: string): string {
  if (root.volumeRoot === "/") return absolute.replace(/^\//, "");
  if (!absolute.startsWith(root.volumeRoot)) {
    throw new Error(`${absolute} is not on the volume of ${root.dir}; Serato can only reference files on the same drive as the _Serato_ folder`);
  }
  return absolute.slice(root.volumeRoot.length);
}

export function rootForPath(roots: SeratoRoot[], absolute: string): SeratoRoot {
  const candidates = roots.filter((r) => r.volumeRoot === "/" || absolute.startsWith(r.volumeRoot));
  candidates.sort((a, b) => b.volumeRoot.length - a.volumeRoot.length);
  if (!candidates.length) throw new Error(`No Serato library found for ${absolute}`);
  return candidates[0];
}

// ---- parsing -----------------------------------------------------------------

function parseDuration(s?: string): number | undefined {
  if (!s) return undefined;
  const m = /^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$/.exec(s.trim());
  if (!m) return undefined;
  return Number(m[1] ?? 0) * 3600 + Number(m[2]) * 60 + Number(m[3]);
}

export function parseEnergy(...sources: (string | undefined)[]): number | undefined {
  for (const s of sources) {
    const m = s && /energy\s*[:\-]?\s*(\d{1,2})/i.exec(s);
    if (m) {
      const n = Number(m[1]);
      if (n >= 1 && n <= 10) return n;
    }
  }
  return undefined;
}

function colorFromU32(n: unknown): string | undefined {
  if (typeof n !== "number") return undefined;
  return "#" + (n & 0xffffff).toString(16).padStart(6, "0");
}

function trackFromField(root: SeratoRoot, f: Field): Track | undefined {
  const v: Record<string, unknown> = {};
  for (const c of f.children ?? []) v[c.tag] = readValue(c);
  const stored = v.pfil as string | undefined;
  if (!stored) return undefined;
  const path = toAbsolute(root, stored);
  const rawKey = (v.tkey as string) || undefined;
  const cam: Camelot | undefined = parseKey(rawKey) ?? parseKey(/^\s*(\d{1,2}[AB])\b/i.exec((v.tcom as string) ?? "")?.[1]);
  const bpm = parseFloat(v.tbpm as string);
  const split = splitArtistTitle(v.tart as string, v.tsng as string);
  const added = typeof v.uadd === "number" ? v.uadd : Number(v.tadd);
  return {
    path,
    storedPath: stored,
    root: root.dir,
    fileType: v.ttyp as string,
    title: split.title,
    artist: split.artist,
    album: (v.talb as string) || undefined,
    genre: (v.tgen as string) || undefined,
    comment: (v.tcom as string) || undefined,
    grouping: (v.tgrp as string) || undefined,
    remixer: (v.trmx as string) || undefined,
    composer: (v.tcmp as string) || undefined,
    label: (v.tlbl as string) || undefined,
    year: (v.ttyr as string) || undefined,
    bpm: Number.isFinite(bpm) && bpm > 0 ? bpm : undefined,
    key: rawKey,
    camelot: cam && camelotString(cam),
    keyName: cam && keyName(cam),
    energy: parseEnergy(v.tcom as string, v.tgrp as string),
    durationSec: parseDuration(v.tlen as string),
    bitrate: v.tbit as string,
    sampleRate: v.tsmp as string,
    sizeBytes: typeof v.ufsb === "number" ? v.ufsb : undefined,
    color: colorFromU32(v.ulbl),
    addedAt: Number.isFinite(added) && added > 0 ? new Date(added * 1000).toISOString() : undefined,
    played: v.bply === true,
    missing: !existsSync(path),
    corrupt: v.bcrt === true,
    beatgridLocked: v.bbgl === true,
    crates: [],
  };
}

export function crateDisplayName(fileName: string): string {
  return fileName.replace(/\.s?crate$/, "").split("%%").join(" / ");
}

export function crateFileName(displayName: string): string {
  const parts = displayName.split("/").map((p) => p.trim()).filter(Boolean);
  if (!parts.length) throw new Error("Crate name is empty");
  for (const p of parts) if (/[\\:%]/.test(p)) throw new Error(`Crate name part "${p}" contains an unsupported character (\\ : %)`);
  return parts.join("%%") + ".crate";
}

function parseCrate(root: SeratoRoot, file: string, smart: boolean): Crate {
  const fields = decode(readFileSync(file));
  const trackPaths: string[] = [];
  const columns: string[] = [];
  for (const f of fields) {
    if (f.tag === "otrk") {
      const p = getChild(f, "ptrk");
      if (p) trackPaths.push(toAbsolute(root, decodeUtf16BE(p.raw)));
    } else if (f.tag === "ovct") {
      const n = getChild(f, "tvcn");
      if (n) columns.push(decodeUtf16BE(n.raw));
    }
  }
  const fileName = basename(file);
  return { name: crateDisplayName(fileName), fileName, file, root: root.dir, smart, trackPaths, columns };
}

// ---- history -----------------------------------------------------------------

/** Serato history `adat` records use numeric field ids instead of ASCII tags. */
const ADAT_FIELDS: Record<number, { name: keyof HistoryEntry | "row" | "location" | "filename"; type: "str" | "u32" | "bool" }> = {
  1: { name: "row", type: "u32" },
  2: { name: "path", type: "str" },
  3: { name: "location", type: "str" },
  4: { name: "filename", type: "str" },
  6: { name: "title", type: "str" },
  7: { name: "artist", type: "str" },
  15: { name: "bpm", type: "str" },
  28: { name: "startTime", type: "u32" },
  29: { name: "endTime", type: "u32" },
  31: { name: "deck", type: "u32" },
  45: { name: "playTimeSec", type: "u32" },
  50: { name: "played", type: "bool" },
  51: { name: "key", type: "str" },
};

function parseAdat(buf: Buffer, session: string): HistoryEntry {
  const e: Record<string, unknown> = { session };
  let pos = 0;
  while (pos + 8 <= buf.length) {
    const id = buf.readUInt32BE(pos);
    const len = buf.readUInt32BE(pos + 4);
    const raw = buf.subarray(pos + 8, pos + 8 + len);
    pos += 8 + len;
    const spec = ADAT_FIELDS[id];
    if (!spec) continue;
    if (spec.type === "str") e[spec.name] = decodeUtf16BE(raw).replace(/\0+$/, "");
    else if (spec.type === "u32" && raw.length === 4) e[spec.name] = raw.readUInt32BE(0);
    else if (spec.type === "bool" && raw.length >= 1) e[spec.name] = raw[0] !== 0;
  }
  for (const k of ["startTime", "endTime"] as const) {
    const n = e[k] as unknown;
    if (typeof n === "number" && n > 0) e[k] = new Date(n * 1000).toISOString();
    else delete e[k];
  }
  delete e.row;
  delete e.location;
  delete e.filename;
  return e as unknown as HistoryEntry;
}

export function parseSessionFile(file: string): Session {
  const buf = readFileSync(file);
  const name = basename(file).replace(/\.session$/, "");
  const entries: HistoryEntry[] = [];
  let pos = 0;
  while (pos + 8 <= buf.length) {
    const tag = buf.toString("latin1", pos, pos + 4);
    const len = buf.readUInt32BE(pos + 4);
    const body = buf.subarray(pos + 8, pos + 8 + len);
    pos += 8 + len;
    if (tag !== "oent") continue;
    let p = 0;
    while (p + 8 <= body.length) {
      const t = body.toString("latin1", p, p + 4);
      const l = body.readUInt32BE(p + 4);
      if (t === "adat") entries.push(parseAdat(body.subarray(p + 8, p + 8 + l), name));
      p += 8 + l;
    }
  }
  const starts = entries.map((e) => e.startTime).filter(Boolean).sort() as string[];
  return { file, name, startedAt: starts[0], modifiedAt: statSync(file).mtime.toISOString(), entries };
}

// ---- library -----------------------------------------------------------------

export class Library {
  roots: SeratoRoot[];
  tracks: Track[] = [];
  crates: Crate[] = [];
  private byPath = new Map<string, Track>();
  private signature = "";

  constructor(roots = discoverRoots()) {
    this.roots = roots;
  }

  /** "sqlite" for Serato DJ 4+ (live library), "legacy" for database V2 + crate files. */
  get backend(): "sqlite" | "legacy" {
    return hasSqliteLibrary() ? "sqlite" : "legacy";
  }

  /** Reload when any Serato file changed since the last read. */
  refresh(force = false): this {
    const sig = this.computeSignature();
    if (!force && sig === this.signature) return this;
    this.signature = sig;
    this.byPath.clear();
    const legacy = this.loadLegacy();
    if (this.backend === "sqlite") {
      const snap = loadSqlite();
      this.tracks = snap.tracks;
      this.crates = snap.crates;
      // Crates written by serato-mcp (or Serato 3) that Serato 4 hasn't imported yet.
      const known = new Set(this.crates.filter((c) => c.space === "Serato Library").map((c) => c.name.toLowerCase()));
      for (const c of legacy.crates) {
        const sqliteTwin = this.crates.find((x) => x.space === "Serato Library" && x.name.toLowerCase() === c.name.toLowerCase());
        if (sqliteTwin) sqliteTwin.file = c.file;
        else if (!known.has(c.name.toLowerCase())) this.crates.push({ ...c, kind: "crate", space: "Serato Library", pendingImport: true });
      }
    } else {
      this.tracks = legacy.tracks;
      this.crates = legacy.crates;
    }
    for (const t of this.tracks) if (!this.byPath.has(t.path)) this.byPath.set(t.path, t);
    for (const c of this.crates) {
      if (!c.pendingImport && this.backend === "sqlite") continue; // memberships already set
      for (const p of c.trackPaths) {
        const t = this.byPath.get(p);
        if (t && !t.crates.includes(c.name)) t.crates.push(c.name);
      }
    }
    return this;
  }

  /** Tracks and crates from the legacy `_Serato_` files (database V2, Subcrates). */
  loadLegacy(): { tracks: Track[]; crates: Crate[] } {
    const tracks: Track[] = [];
    const crates: Crate[] = [];
    const seen = new Set<string>();
    for (const root of this.roots) {
      const db = join(root.dir, "database V2");
      if (existsSync(db)) {
        for (const f of decode(readFileSync(db))) {
          if (f.tag !== "otrk") continue;
          const t = trackFromField(root, f);
          if (t && !seen.has(t.path)) {
            seen.add(t.path);
            tracks.push(t);
          }
        }
      }
      for (const [sub, smart] of [["Subcrates", false], ["SmartCrates", true]] as const) {
        const dir = join(root.dir, sub);
        if (!existsSync(dir)) continue;
        for (const file of readdirSync(dir)) {
          if (!/\.s?crate$/.test(file)) continue;
          try {
            crates.push({ ...parseCrate(root, join(dir, file), smart), kind: smart ? "smart crate" : "crate" });
          } catch (err) {
            process.stderr.write(`serato-mcp: could not read crate ${file}: ${err}\n`);
          }
        }
      }
    }
    return { tracks, crates };
  }

  private computeSignature(): string {
    const parts: string[] = [hasSqliteLibrary() ? sqliteSignature() : "legacy"];
    for (const r of this.roots) {
      for (const p of [join(r.dir, "database V2"), join(r.dir, "Subcrates"), join(r.dir, "SmartCrates")]) {
        try {
          const st = statSync(p);
          parts.push(`${p}:${st.mtimeMs}:${st.size}`);
          if (st.isDirectory()) for (const f of readdirSync(p)) parts.push(f + ":" + statSync(join(p, f)).mtimeMs);
        } catch {
          parts.push(p + ":none");
        }
      }
    }
    return parts.join("|");
  }

  track(path: string): Track | undefined {
    return this.byPath.get(path);
  }

  /** Tracks a DJ can load: local files that exist, plus streaming tracks when asked. */
  playable(opts: { includeStreaming?: boolean; includeMissing?: boolean } = {}): Track[] {
    return this.tracks.filter((t) => {
      if (t.source?.startsWith("streaming")) return !!opts.includeStreaming;
      if (/\/_Serato_\/Imported\//.test(t.path)) return false; // bundled samples
      return opts.includeMissing || !t.missing;
    });
  }

  /** Resolve a user reference: absolute path, stored path, or a fuzzy "artist - title" query. */
  resolve(ref: string): Track {
    const exact = this.byPath.get(ref) ?? this.tracks.find((t) => t.storedPath === ref || "/" + t.storedPath === ref);
    if (exact) return exact;
    const hits = this.search(ref, 2);
    if (hits.length === 0) throw new Error(`No track matches "${ref}"`);
    return hits[0];
  }

  search(query: string, limit = 50, pool: Track[] = this.tracks): Track[] {
    const terms = normalize(query).split(" ").filter(Boolean);
    if (!terms.length) return pool.slice(0, limit);
    const scored: { t: Track; s: number }[] = [];
    for (const t of pool) {
      const title = normalize(t.title ?? basename(t.path));
      const artist = normalize(t.artist ?? "");
      const hay = `${artist} ${title} ${normalize(t.album ?? "")} ${normalize(t.genre ?? "")} ${normalize(t.remixer ?? "")} ${normalize(t.label ?? "")} ${normalize(basename(t.path))}`;
      if (!terms.every((w) => hay.includes(w))) continue;
      let s = 0;
      for (const w of terms) {
        if (title.includes(w)) s += 3;
        if (artist.includes(w)) s += 2;
      }
      if (title.startsWith(terms.join(" "))) s += 2;
      if (t.missing) s -= 1;
      if (t.source?.startsWith("streaming")) s -= 2;
      scored.push({ t, s });
    }
    scored.sort((a, b) => b.s - a.s);
    return scored.slice(0, limit).map((x) => x.t);
  }

  crate(name: string): Crate {
    const n = name.trim().toLowerCase();
    const c =
      this.crates.find((c) => c.name.toLowerCase() === n || c.fileName.toLowerCase() === n || c.fileName.toLowerCase() === n + ".crate") ??
      this.crates.find((c) => c.name.toLowerCase().endsWith("/ " + n));
    if (!c) throw new Error(`No crate named "${name}". Crates: ${this.crates.map((c) => c.name).join(", ")}`);
    return c;
  }

  sessions(): Session[] {
    if (this.backend === "sqlite") return loadSqliteSessions();
    const out: Session[] = [];
    for (const r of this.roots) {
      const dir = join(r.dir, "History", "Sessions");
      if (!existsSync(dir)) continue;
      for (const f of readdirSync(dir)) {
        if (!f.endsWith(".session")) continue;
        try {
          out.push(parseSessionFile(join(dir, f)));
        } catch (err) {
          process.stderr.write(`serato-mcp: could not read session ${f}: ${err}\n`);
        }
      }
    }
    return out.sort((a, b) => (b.startedAt ?? b.modifiedAt).localeCompare(a.startedAt ?? a.modifiedAt));
  }
}

export function normalize(s: string): string {
  return s
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9#]+/g, " ")
    .trim();
}
