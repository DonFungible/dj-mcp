/**
 * Safe writes to Serato's library files.
 *
 * Serato keeps its library in memory and rewrites `database V2` and crate files
 * on exit, so edits made while it runs are lost or can corrupt the library.
 * Every write here therefore:
 *   1. refuses while Serato DJ is running (unless `force`),
 *   2. copies each file it is about to change into ~/.serato-mcp/backups/<stamp>/,
 *   3. writes atomically (temp file + rename).
 */
import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync, readdirSync, statSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, relative } from "node:path";
import { decode, decodeUtf16BE, encode, encodeUtf16BE, type Field, getChild, makeField, setChild, removeChild } from "./binary.js";
import { crateFileName, type Library, rootForPath, type SeratoRoot, toStored } from "./library.js";

export function seratoRunning(): boolean {
  try {
    const out = execFileSync("pgrep", ["-f", "Serato DJ (Pro|Lite)\\.app/Contents/MacOS/"], { encoding: "utf8" });
    return out.trim().length > 0;
  } catch {
    return false;
  }
}

export function assertWritable(force?: boolean): void {
  if (process.env.SERATO_MCP_READONLY === "1") throw new Error("serato-mcp is in read-only mode (SERATO_MCP_READONLY=1)");
  if (!force && seratoRunning()) {
    throw new Error(
      "Serato DJ is running. It overwrites library files when it quits, so changes made now would be lost or could corrupt the library. " +
        "Quit Serato first (the quit_serato tool can do it), or pass force: true if you accept the risk."
    );
  }
}

export const backupBase = () => process.env.SERATO_MCP_BACKUP_DIR ?? join(homedir(), ".serato-mcp", "backups");

export class WriteTxn {
  readonly stamp = new Date().toISOString().replace(/[:.]/g, "-");
  readonly backupDir = join(backupBase(), this.stamp);
  readonly changed: string[] = [];
  private backedUp = new Set<string>();

  backup(file: string): void {
    if (this.backedUp.has(file) || !existsSync(file)) return;
    const rel = relative("/", file);
    const dest = join(this.backupDir, rel);
    mkdirSync(dirname(dest), { recursive: true });
    copyFileSync(file, dest);
    this.backedUp.add(file);
  }

  write(file: string, data: Buffer): void {
    this.backup(file);
    mkdirSync(dirname(file), { recursive: true });
    const tmp = `${file}.serato-mcp-${process.pid}.tmp`;
    writeFileSync(tmp, data);
    renameSync(tmp, file);
    this.changed.push(file);
  }

  /** Delete a file; its backup copy stays in the backup folder. */
  remove(file: string): void {
    this.backup(file);
    unlinkSync(file);
    this.changed.push(file);
  }

  summary() {
    return { changedFiles: this.changed, backupDir: this.backedUp.size ? this.backupDir : undefined };
  }
}

// ---- crates ----------------------------------------------------------------

const DEFAULT_COLUMNS: [string, string][] = [
  ["song", "250"], ["artist", "250"], ["bpm", "30"], ["key", "30"], ["album", "250"], ["length", "250"], ["comment", "250"],
];

function newCrateFields(): Field[] {
  const fields: Field[] = [makeField("vrsn", "1.0/Serato ScratchLive Crate")];
  for (const [name, width] of DEFAULT_COLUMNS) {
    fields.push({ tag: "ovct", raw: Buffer.alloc(0), children: [makeField("tvcn", name), makeField("tvcw", width)] });
  }
  return fields;
}

const trackField = (stored: string): Field => ({ tag: "otrk", raw: Buffer.alloc(0), children: [makeField("ptrk", stored)] });

function crateStoredPaths(fields: Field[]): string[] {
  return fields.filter((f) => f.tag === "otrk").map((f) => getChild(f, "ptrk")).filter(Boolean).map((p) => decodeUtf16BE(p!.raw));
}

/** Group absolute track paths by the Serato root (drive) they belong to. */
function groupByRoot(lib: Library, paths: string[]): Map<SeratoRoot, string[]> {
  const out = new Map<SeratoRoot, string[]>();
  for (const p of paths) {
    const r = rootForPath(lib.roots, p);
    out.set(r, [...(out.get(r) ?? []), p]);
  }
  return out;
}

function updateCrateOrder(txn: WriteTxn, root: SeratoRoot, displayName: string, op: "add" | "remove", renameTo?: string): void {
  const file = join(root.dir, "neworder.pref");
  if (!existsSync(file)) return;
  const text = decodeUtf16BE(readFileSync(file));
  const line = `[crate]${displayName.split("/").map((s) => s.trim()).join("%%")}`;
  const lines = text.split("\n");
  let next = lines;
  if (op === "add") {
    if (lines.includes(line)) return;
    const end = lines.findIndex((l) => l.startsWith("[end record]"));
    next = [...lines];
    next.splice(end >= 0 ? end : lines.length, 0, line);
  } else if (renameTo) {
    const to = `[crate]${renameTo.split("/").map((s) => s.trim()).join("%%")}`;
    next = lines.map((l) => (l === line || l.startsWith(line + "%%") ? to + l.slice(line.length) : l));
  } else {
    next = lines.filter((l) => !(l === line || l.startsWith(line + "%%")));
  }
  txn.write(file, encodeUtf16BE(next.join("\n")));
}

export interface CrateWriteResult {
  crate: string;
  files: string[];
  added?: number;
  removed?: number;
  skippedDuplicates?: number;
  totalTracks?: number;
  backupDir?: string;
}

export function writeCrateTracks(
  lib: Library,
  name: string,
  trackPaths: string[],
  mode: "create" | "replace" | "append",
  opts: { force?: boolean } = {}
): CrateWriteResult {
  assertWritable(opts.force);
  const fileName = crateFileName(name);
  const txn = new WriteTxn();
  const streaming = trackPaths.filter((p) => /^[a-z]+:\/\//.test(p));
  trackPaths = trackPaths.filter((p) => !/^[a-z]+:\/\//.test(p));
  const existing = findCrate(lib, name);
  if (existing && existing.space && existing.space !== "Serato Library") throw new Error(`"${existing.name}" is a ${existing.space} ${existing.kind}; only Serato crates can be edited`);
  if (mode === "create" && existing) throw new Error(`Crate "${existing.name}" already exists. Use mode "append" or "replace".`);
  // Serato 4 crate never exported to a legacy file: start from its current tracks so the import doesn't drop them.
  if (mode === "append" && existing && !legacyCrateFiles(lib, fileName).length) trackPaths = [...existing.trackPaths.filter((p) => !/^[a-z]+:\/\//.test(p)), ...trackPaths];
  const groups = groupByRoot(lib, trackPaths);
  // A crate with no tracks still needs a file on the home library.
  if (groups.size === 0) groups.set(lib.roots[0], []);
  let added = 0;
  let skipped = 0;
  let total = 0;
  for (const [root, paths] of groups) {
    const file = join(root.dir, "Subcrates", fileName);
    const exists = existsSync(file);
    if (mode === "create" && exists) throw new Error(`Crate "${name}" already exists. Use mode "append" or "replace".`);
    let fields = exists && mode === "append" ? decode(readFileSync(file)) : newCrateFields();
    if (exists && mode === "replace") {
      // Keep the user's column layout.
      const old = decode(readFileSync(file));
      fields = old.filter((f) => f.tag !== "otrk");
    }
    const present = new Set(crateStoredPaths(fields));
    for (const p of paths) {
      const stored = toStored(root, p);
      if (present.has(stored)) {
        skipped++;
        continue;
      }
      present.add(stored);
      fields.push(trackField(stored));
      added++;
    }
    total += present.size;
    txn.write(file, encode(fields));
    if (!exists) {
      // Make sure parent crates exist so Serato shows the nesting.
      const parts = fileName.replace(/\.crate$/, "").split("%%");
      for (let i = 1; i < parts.length; i++) {
        const parentFile = join(root.dir, "Subcrates", parts.slice(0, i).join("%%") + ".crate");
        if (!existsSync(parentFile)) txn.write(parentFile, encode(newCrateFields()));
      }
      updateCrateOrder(txn, root, name, "add");
    }
  }
  lib.refresh(true);
  return {
    crate: name,
    files: txn.changed,
    added,
    skippedDuplicates: skipped,
    totalTracks: total,
    backupDir: txn.summary().backupDir,
    ...(streaming.length ? { skippedStreamingTracks: streaming.length } : {}),
    ...importNote(lib),
  };
}

function findCrate(lib: Library, name: string) {
  try {
    return lib.crate(name);
  } catch {
    return undefined;
  }
}

/** Legacy crate files (one per drive) for a crate file name. */
function legacyCrateFiles(lib: Library, fileName: string): { root: SeratoRoot; file: string }[] {
  return lib.roots.map((root) => ({ root, file: join(root.dir, "Subcrates", fileName) })).filter((x) => existsSync(x.file));
}

function editableCrate(lib: Library, name: string) {
  const crate = lib.crate(name);
  if (crate.space && crate.space !== "Serato Library") throw new Error(`"${crate.name}" is a ${crate.space} ${crate.kind}; only Serato crates can be edited`);
  const fileName = crate.fileName || crateFileName(crate.name);
  const files = legacyCrateFiles(lib, fileName);
  return { crate, fileName, files };
}

function importNote(lib: Library) {
  return lib.backend === "sqlite"
    ? { note: "Written to Serato's library files. Serato DJ 4 imports the change the next time it starts; until then the crate shows as pendingImport." }
    : {};
}

export function removeFromCrate(lib: Library, name: string, trackPaths: string[], opts: { force?: boolean } = {}): CrateWriteResult {
  assertWritable(opts.force);
  const { crate, files } = editableCrate(lib, name);
  if (!files.length) {
    // Serato 4 crate without a legacy file: write one with the remaining tracks.
    const drop = new Set(trackPaths);
    return { ...writeCrateTracks(lib, crate.name, crate.trackPaths.filter((p) => !drop.has(p)), "replace", opts), removed: crate.trackPaths.filter((p) => drop.has(p)).length };
  }
  const txn = new WriteTxn();
  let removed = 0;
  let total = 0;
  for (const { root, file } of files) {
    const drop = new Set(trackPaths.filter((p) => !/^[a-z]+:\/\//.test(p) && rootForPath(lib.roots, p) === root).map((p) => toStored(root, p)));
    const fields = decode(readFileSync(file));
    const kept = fields.filter((f) => {
      if (f.tag !== "otrk") return true;
      const p = getChild(f, "ptrk");
      const hit = p && drop.has(decodeUtf16BE(p.raw));
      if (hit) removed++;
      return !hit;
    });
    total += kept.filter((f) => f.tag === "otrk").length;
    if (kept.length !== fields.length) txn.write(file, encode(kept));
  }
  lib.refresh(true);
  return { crate: crate.name, files: txn.changed, removed, totalTracks: total, backupDir: txn.summary().backupDir, ...importNote(lib) };
}

export function deleteCrate(lib: Library, name: string, opts: { force?: boolean } = {}): CrateWriteResult {
  assertWritable(opts.force);
  const { crate, fileName, files } = editableCrate(lib, name);
  if (!files.length) throw new Error(`"${crate.name}" has no crate file in _Serato_/Subcrates to delete. Delete it inside Serato DJ instead.`);
  const prefix = fileName.replace(/\.crate$/, "");
  const txn = new WriteTxn();
  for (const root of lib.roots) {
    const dir = join(root.dir, "Subcrates");
    if (!existsSync(dir)) continue;
    for (const f of readdirSync(dir)) {
      if (f === fileName || f.startsWith(prefix + "%%")) txn.remove(join(dir, f));
    }
    updateCrateOrder(txn, root, crate.name.split(" / ").join("/"), "remove");
  }
  lib.refresh(true);
  return { crate: crate.name, files: txn.changed, backupDir: txn.summary().backupDir, ...importNote(lib) };
}

export function renameCrate(lib: Library, name: string, newName: string, opts: { force?: boolean } = {}): CrateWriteResult {
  assertWritable(opts.force);
  const { crate, fileName, files } = editableCrate(lib, name);
  if (!files.length) throw new Error(`"${crate.name}" has no crate file in _Serato_/Subcrates to rename. Rename it inside Serato DJ instead.`);
  const oldPrefix = fileName.replace(/\.crate$/, "");
  const newPrefix = crateFileName(newName).replace(/\.crate$/, "");
  const txn = new WriteTxn();
  for (const root of lib.roots) {
    const dir = join(root.dir, "Subcrates");
    if (!existsSync(dir)) continue;
    for (const f of readdirSync(dir)) {
      if (f === fileName || f.startsWith(oldPrefix + "%%")) {
        const target = join(dir, newPrefix + f.slice(oldPrefix.length));
        if (existsSync(target)) throw new Error(`A crate already exists at ${basename(target)}`);
        txn.backup(join(dir, f));
        renameSync(join(dir, f), target);
        txn.changed.push(target);
      }
    }
    updateCrateOrder(txn, root, crate.name.split(" / ").join("/"), "remove", newName);
  }
  lib.refresh(true);
  return { crate: newName, files: txn.changed, backupDir: txn.summary().backupDir, ...importNote(lib) };
}

// ---- database V2 ------------------------------------------------------------

export const EDITABLE_FIELDS = {
  title: "tsng", artist: "tart", album: "talb", genre: "tgen", comment: "tcom", grouping: "tgrp",
  remixer: "trmx", composer: "tcmp", label: "tlbl", year: "ttyr", key: "tkey", bpm: "tbpm",
} as const;

export type TrackEdits = Partial<Record<keyof typeof EDITABLE_FIELDS, string>> & { color?: string };

function dbTrackFields(fields: Field[], stored: string): Field | undefined {
  return fields.find((f) => f.tag === "otrk" && getChild(f, "pfil") && decodeUtf16BE(getChild(f, "pfil")!.raw) === stored);
}

export function editTracks(lib: Library, edits: { path: string; changes: TrackEdits }[], opts: { force?: boolean } = {}) {
  assertWritable(opts.force);
  const txn = new WriteTxn();
  const results: { path: string; updated: string[] }[] = [];
  const byRoot = new Map<SeratoRoot, typeof edits>();
  for (const e of edits) {
    const r = rootForPath(lib.roots, e.path);
    byRoot.set(r, [...(byRoot.get(r) ?? []), e]);
  }
  for (const [root, list] of byRoot) {
    const file = join(root.dir, "database V2");
    const fields = decode(readFileSync(file));
    for (const e of list) {
      const t = dbTrackFields(fields, toStored(root, e.path));
      if (!t)
        throw new Error(
          lib.backend === "sqlite"
            ? `${e.path} is only in Serato DJ 4's library, not in the legacy database V2 that serato-mcp can edit. Edit it inside Serato (or its file tags) instead.`
            : `Track not in Serato database: ${e.path}`
        );
      const updated: string[] = [];
      for (const [k, v] of Object.entries(e.changes)) {
        if (v === undefined) continue;
        if (k === "color") {
          const hex = /^#?([0-9a-f]{6})$/i.exec(v);
          if (!hex) throw new Error(`color must be #rrggbb, got ${v}`);
          setChild(t, "ulbl", parseInt(hex[1], 16));
        } else {
          const tag = EDITABLE_FIELDS[k as keyof typeof EDITABLE_FIELDS];
          if (!tag) throw new Error(`Field ${k} is not editable`);
          if (k === "bpm" && v !== "" && !Number.isFinite(Number(v))) throw new Error(`bpm must be a number, got ${v}`);
          if (v === "") removeChild(t, tag);
          else setChild(t, tag, k === "bpm" ? Number(v).toFixed(2) : v);
        }
        updated.push(k);
      }
      results.push({ path: e.path, updated });
    }
    txn.write(file, encode(fields));
  }
  lib.refresh(true);
  return { results, ...txn.summary(), ...importNote(lib) };
}

/**
 * Find moved/renamed files for tracks Serato reports as missing and repoint the
 * database and every crate at the new location. Matches by file name, using the
 * stored file size to break ties.
 */
export function relocateMissing(lib: Library, searchDirs: string[], opts: { dryRun: boolean; force?: boolean }) {
  const missing = lib.tracks.filter((t) => t.missing);
  const index = new Map<string, string[]>();
  const walk = (dir: string, depth: number) => {
    if (depth > 8) return;
    let entries: string[];
    try {
      entries = readdirSync(dir);
    } catch {
      return;
    }
    for (const e of entries) {
      if (e.startsWith(".") || e === "_Serato_" || e === "_Serato_Backup") continue;
      const full = join(dir, e);
      let st;
      try {
        st = statSync(full);
      } catch {
        continue;
      }
      if (st.isDirectory()) walk(full, depth + 1);
      else if (/\.(mp3|m4a|mp4|aac|alac|flac|wav|wave|aif|aiff|ogg)$/i.test(e)) {
        const k = matchKey(e);
        index.set(k, [...(index.get(k) ?? []), full]);
      }
    }
  };
  for (const d of searchDirs) walk(d, 0);

  const matches: { track: string; from: string; to: string; confidence: "exact-name" | "name+size" | "fuzzy-name" }[] = [];
  const unresolved: string[] = [];
  for (const t of missing) {
    let cands = index.get(matchKey(basename(t.path))) ?? [];
    let confidence: "exact-name" | "name+size" | "fuzzy-name" = "exact-name";
    if (!cands.length) {
      // Same name without the "(Extended Mix)"-style suffix, e.g. "Eynka - Symphony.mp3"
      const loose = looseKey(basename(t.path));
      cands = [...index.entries()].filter(([k]) => looseKey(k) === loose).flatMap(([, v]) => v);
      confidence = "fuzzy-name";
    }
    if (cands.length > 1 && t.sizeBytes) {
      const sized = cands.filter((c) => statSync(c).size === t.sizeBytes);
      if (sized.length >= 1) {
        cands = sized;
        if (confidence === "exact-name") confidence = "name+size";
      }
    }
    if (cands.length === 1 && !matches.some((m) => m.to === cands[0])) matches.push({ track: `${t.artist ?? ""} - ${t.title ?? basename(t.path)}`, from: t.path, to: cands[0], confidence });
    else unresolved.push(t.path);
  }
  if (opts.dryRun || !matches.length) return { dryRun: opts.dryRun, missing: missing.length, matched: matches, unresolved };

  assertWritable(opts.force);
  const txn = new WriteTxn();
  for (const root of lib.roots) {
    const map = new Map<string, string>();
    for (const m of matches) {
      if (rootForPath(lib.roots, m.from) !== root) continue;
      try {
        map.set(toStored(root, m.from), toStored(root, m.to));
      } catch {
        unresolved.push(m.from); // moved to another drive: Serato can't follow that
      }
    }
    if (!map.size) continue;
    const db = join(root.dir, "database V2");
    if (existsSync(db)) {
      const fields = decode(readFileSync(db));
      const existing = new Set(fields.filter((f) => f.tag === "otrk").map((f) => decodeUtf16BE(getChild(f, "pfil")!.raw)));
      for (const f of fields) {
        if (f.tag !== "otrk") continue;
        const p = getChild(f, "pfil");
        const to = p && map.get(decodeUtf16BE(p.raw));
        if (to && !existing.has(to)) setChild(f, "pfil", to);
      }
      // If the new location is already its own entry, drop the stale duplicate.
      const cleaned = fields.filter((f) => {
        if (f.tag !== "otrk") return true;
        const stored = decodeUtf16BE(getChild(f, "pfil")!.raw);
        return !(map.has(stored) && existing.has(map.get(stored)!));
      });
      txn.write(db, encode(cleaned));
    }
    const dir = join(root.dir, "Subcrates");
    if (existsSync(dir)) {
      for (const cf of readdirSync(dir).filter((f) => f.endsWith(".crate"))) {
        const file = join(dir, cf);
        const fields = decode(readFileSync(file));
        let touched = false;
        for (const f of fields) {
          if (f.tag !== "otrk") continue;
          const p = getChild(f, "ptrk");
          const to = p && map.get(decodeUtf16BE(p.raw));
          if (to) {
            setChild(f, "ptrk", to);
            touched = true;
          }
        }
        if (touched) txn.write(file, encode(fields));
      }
    }
  }
  lib.refresh(true);
  return { dryRun: false, missing: missing.length, relocated: matches, unresolved, ...txn.summary() };
}

const matchKey = (name: string) => name.normalize("NFC").toLowerCase().replace(/[\s_]+/g, " ");
const looseKey = (name: string) =>
  matchKey(name)
    .replace(/\.[a-z0-9]+$/, "")
    .replace(/\((original|extended|clean extended|radio|dirty extended)( mix| edit)?\)/g, "")
    .replace(/[^a-z0-9]+/g, "");
