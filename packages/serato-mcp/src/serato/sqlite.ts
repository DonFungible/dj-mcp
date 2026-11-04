/**
 * Serato DJ 4+ library: ~/Library/Application Support/Serato/Library/master.sqlite.
 *
 * master.sqlite aggregates one database per location (root.sqlite for the boot
 * drive, one per external drive, spotify.sqlite for streaming). It is opened
 * read-only; Serato uses WAL mode so reading while it runs is safe.
 */
import { DatabaseSync } from "node:sqlite";
import { existsSync, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { camelotString, keyName, parseKey } from "../mix/keys.js";
import { parseEnergy, type Crate, type HistoryEntry, type Session, type Track } from "./library.js";

export const sqliteDir = () => process.env.SERATO_SQLITE_DIR ?? join(homedir(), "Library", "Application Support", "Serato", "Library");
export const masterPath = () => join(sqliteDir(), "master.sqlite");
export const hasSqliteLibrary = () => process.env.SERATO_BACKEND !== "legacy" && existsSync(masterPath());

/** Serato appends U+105E8A + an id to some container names, e.g. "DonJenk\u{105E8A}1". */
const cleanName = (n: string) => n.replace(/\u{105E8A}.*$/u, "");

const CONTAINER_KIND: Record<number, Crate["kind"]> = {
  1: "crate", 2: "smart crate", 3: "playlist", 4: "playlist folder", 16: "streaming folder", 17: "streaming playlist", 18: "streaming playlist", 23: "streaming playlist",
};

function open(): DatabaseSync {
  return new DatabaseSync(masterPath(), { readOnly: true });
}

interface LocationInfo {
  id: number;
  root?: string; // directory portable_ids are relative to
  streaming: boolean;
}

function locations(db: DatabaseSync): Map<number, LocationInfo> {
  const out = new Map<number, LocationInfo>();
  const rows = db.prepare("SELECT l.id AS id, c.database_uri AS uri FROM location l LEFT JOIN connection c ON c.location_id = l.id").all() as { id: number; uri: string | null }[];
  for (const r of rows) {
    const uri = r.uri ?? "";
    if (/spotify|tidal|soundcloud|beatport|beatsource|streaming/i.test(uri)) out.set(r.id, { id: r.id, streaming: true });
    else if (uri.endsWith("/root.sqlite")) out.set(r.id, { id: r.id, root: "/", streaming: false });
    else {
      // External drive: its database lives on the drive at <mount>/_Serato_/Library/location.sqlite.
      const m = /^(\/Volumes\/[^/]+)\//.exec(uri);
      out.set(r.id, { id: r.id, root: m ? m[1] + "/" : undefined, streaming: false });
    }
  }
  return out;
}

let volumes: string[] | undefined;
function guessVolumeRoot(portable: string): string | undefined {
  volumes ??= existsSync("/Volumes") ? readdirSync("/Volumes").map((v) => `/Volumes/${v}/`) : [];
  return volumes.find((v) => existsSync(v + portable));
}

export interface SqliteSnapshot {
  tracks: Track[];
  crates: Crate[];
}

export function loadSqlite(): SqliteSnapshot {
  const db = open();
  try {
    const locs = locations(db);
    const assets = db
      .prepare(
        `SELECT id, location_id, portable_id, file_name, file_size, file_bit_rate, file_sample_rate, format, artist, color, comments, grouping,
                remixer, name, album, composer, year, genre, key, label, rating, bpm, length_ms, time_added, is_corrupt, is_missing,
                is_beatgrid_locked, third_party_type, dj_play_count, dj_recently_played
         FROM asset WHERE type = 'audio' OR type = ''`
      )
      .all() as Record<string, any>[];
    const tracks: Track[] = [];
    const byId = new Map<number, Track>();
    for (const a of assets) {
      const loc = locs.get(a.location_id);
      const portable: string = a.portable_id;
      const streaming = loc?.streaming || /^[a-z]+:\/\//.test(portable);
      let path: string;
      if (streaming) path = portable;
      else {
        const root = loc?.root ?? guessVolumeRoot(portable) ?? "/";
        path = portable.startsWith("/") ? portable : root + portable;
      }
      const cam = parseKey(a.key) ?? parseKey(/^\s*(\d{1,2}[AB])\b/i.exec(a.comments ?? "")?.[1]);
      const split = splitArtistTitle(a.artist, a.name);
      const t: Track = {
        id: a.id,
        path,
        storedPath: portable,
        root: streaming ? "streaming" : loc?.root ?? "/",
        source: streaming ? `streaming:${/^streaming:\/\/([a-z]+)/.exec(portable)?.[1] ?? "unknown"}` : a.third_party_type === 1 ? "itunes" : "local",
        fileType: a.format || undefined,
        title: split.title,
        artist: split.artist,
        album: a.album || undefined,
        genre: a.genre || undefined,
        comment: a.comments || undefined,
        grouping: a.grouping || undefined,
        remixer: a.remixer || undefined,
        composer: a.composer || undefined,
        label: a.label || undefined,
        year: a.year || undefined,
        bpm: a.bpm > 0 ? Math.round(a.bpm * 100) / 100 : undefined,
        key: a.key || undefined,
        camelot: cam && camelotString(cam),
        keyName: cam && keyName(cam),
        energy: parseEnergy(a.comments, a.grouping),
        durationSec: a.length_ms ? a.length_ms / 1000 : undefined,
        bitrate: a.file_bit_rate ? `${Math.round(a.file_bit_rate / 1000)}kbps` : undefined,
        sampleRate: a.file_sample_rate ? `${a.file_sample_rate / 1000}k` : undefined,
        sizeBytes: a.file_size ?? undefined,
        color: typeof a.color === "number" ? "#" + (a.color & 0xffffff).toString(16).padStart(6, "0") : undefined,
        rating: typeof a.rating === "number" ? Math.round(a.rating * 5) : undefined,
        playCount: a.dj_play_count ?? 0,
        addedAt: a.time_added ? new Date(a.time_added * 1000).toISOString() : undefined,
        played: !!a.dj_recently_played || (a.dj_play_count ?? 0) > 0,
        missing: streaming ? false : !existsSync(path),
        corrupt: !!a.is_corrupt,
        beatgridLocked: !!a.is_beatgrid_locked,
        crates: [],
      };
      tracks.push(t);
      byId.set(a.id, t);
    }

    const containers = db.prepare("SELECT id, parent_id, name, type, list_order FROM container").all() as { id: number; parent_id: number | null; name: string; type: number; list_order: number }[];
    const cById = new Map(containers.map((c) => [c.id, c]));
    const filenames = new Map(
      (db.prepare("SELECT container_id, filename FROM dj_container_metadata").all() as { container_id: number; filename: string | null }[]).map((r) => [r.container_id, r.filename])
    );
    const rules = new Map((db.prepare("SELECT container_id, rules FROM smart_crate_rules").all() as { container_id: number; rules: string }[]).map((r) => [r.container_id, r.rules]));
    const members = db
      .prepare(
        `SELECT lc.container_id AS cid, ca.asset_id AS aid FROM container_asset ca
         JOIN location_container lc ON lc.id = ca.location_container_id ORDER BY lc.container_id, ca.list_order, ca.id`
      )
      .all() as { cid: number; aid: number }[];
    const byContainer = new Map<number, number[]>();
    for (const m of members) byContainer.set(m.cid, [...(byContainer.get(m.cid) ?? []), m.aid]);

    const pathOf = (c: { id: number; parent_id: number | null; name: string; type: number }): { names: string[]; space: string } => {
      const names: string[] = [];
      let cur: typeof c | undefined = c;
      let space = "";
      while (cur && cur.type !== 0) {
        names.unshift(cleanName(cur.name));
        cur = cur.parent_id != null ? cById.get(cur.parent_id) : undefined;
      }
      if (cur) space = cur.name.replace(/ root$/, "");
      return { names, space };
    };

    const crates: Crate[] = [];
    const sorted = [...containers].sort((a, b) => (a.parent_id ?? -1) - (b.parent_id ?? -1) || a.list_order - b.list_order);
    for (const c of sorted) {
      if (c.type === 0) continue;
      const { names, space } = pathOf(c);
      const kind = CONTAINER_KIND[c.type] ?? "playlist";
      // Internal system containers (e.g. the Stems smart crate) carry an id suffix and no user name.
      if (space === "Stems") continue;
      const isSerato = space === "Serato Library";
      const name = isSerato ? names.join(" / ") : `${space}: ${names.join(" / ")}`;
      const ids = byContainer.get(c.id) ?? [];
      const trackPaths = ids.map((id) => byId.get(id)?.path).filter((p): p is string => !!p);
      const fileName = filenames.get(c.id) ?? (isSerato && kind === "crate" ? names.join("%%") + ".crate" : "");
      crates.push({
        id: c.id,
        name,
        kind,
        space,
        fileName,
        file: "",
        root: sqliteDir(),
        smart: kind === "smart crate",
        rules: rules.has(c.id) ? safeJson(rules.get(c.id)!) : undefined,
        trackPaths,
        columns: [],
      });
    }
    const byPath = new Map(tracks.map((t) => [t.path, t]));
    for (const c of crates) {
      for (const p of c.trackPaths) {
        const t = byPath.get(p);
        if (t && !t.crates.includes(c.name)) t.crates.push(c.name);
      }
    }
    return { tracks, crates };
  } finally {
    db.close();
  }
}

/** Untagged files often carry "Artist - Title" (sometimes "07. Artist - Title") in the title. */
export function splitArtistTitle(artist: string | null | undefined, title: string | null | undefined): { artist?: string; title?: string } {
  const t = (title ?? "").trim();
  if (artist && artist.trim()) return { artist: artist.trim(), title: t || undefined };
  const m = /^(?:\d{1,3}[.\s-]+)?(.+?)\s+-\s+(.+)$/.exec(t);
  if (m) return { artist: m[1].trim(), title: m[2].trim() };
  return { artist: undefined, title: t || undefined };
}

function safeJson(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

export function loadSqliteSessions(limit = 200): Session[] {
  const db = open();
  try {
    const sessions = db.prepare("SELECT id, start_time, end_time, name, notes FROM history_session ORDER BY start_time DESC LIMIT ?").all(limit) as Record<string, any>[];
    const entries = db
      .prepare(
        `SELECT session_id, portable_id, artist, name, bpm, key, start_time, end_time, played, deck, device, location_id
         FROM history_entry WHERE session_id IN (SELECT id FROM history_session ORDER BY start_time DESC LIMIT ?) ORDER BY start_time, id`
      )
      .all(limit) as Record<string, any>[];
    const locs = locations(db);
    const grouped = new Map<number, HistoryEntry[]>();
    for (const e of entries) {
      const loc = locs.get(e.location_id);
      const portable: string = e.portable_id ?? "";
      const path = /^[a-z]+:\/\//.test(portable) || loc?.streaming ? portable : (loc?.root ?? "/") + portable;
      const he: HistoryEntry = {
        session: String(e.session_id),
        path: portable ? path : undefined,
        title: e.name || undefined,
        artist: e.artist || undefined,
        bpm: e.bpm ? String(Math.round(e.bpm * 100) / 100) : undefined,
        key: e.key || undefined,
        deck: e.deck ? Number(e.deck) || undefined : undefined,
        startTime: e.start_time > 0 ? new Date(e.start_time * 1000).toISOString() : undefined,
        endTime: e.end_time > 0 ? new Date(e.end_time * 1000).toISOString() : undefined,
        playTimeSec: e.end_time > 0 && e.start_time > 0 ? e.end_time - e.start_time : undefined,
        played: !!e.played,
        device: e.device || undefined,
      };
      grouped.set(e.session_id, [...(grouped.get(e.session_id) ?? []), he]);
    }
    const masterMtime = statSync(masterPath()).mtime.toISOString();
    return sessions.map((s) => {
      const list = grouped.get(s.id) ?? [];
      const lastEntry = list.reduce((m, e) => Math.max(m, Date.parse(e.endTime ?? e.startTime ?? "0") || 0), 0);
      return {
        file: masterPath(),
        id: s.id,
        name: s.name ? `${s.name} (#${s.id})` : `Session ${s.id}`,
        startedAt: s.start_time ? new Date(s.start_time * 1000).toISOString() : undefined,
        endedAt: s.end_time ? new Date(s.end_time * 1000).toISOString() : undefined,
        modifiedAt: lastEntry ? new Date(lastEntry).toISOString() : masterMtime,
        notes: s.notes || undefined,
        entries: list,
      };
    });
  } finally {
    db.close();
  }
}

/** Is Serato's live library locked by a running Serato process? */
export function sqliteLockOwner(): string | undefined {
  if (!hasSqliteLibrary()) return undefined;
  const db = open();
  try {
    const r = db.prepare("SELECT owner_process_name AS name, owner_process_id AS pid FROM lock").get() as { name: string; pid: number } | undefined;
    return r ? `${r.name} (pid ${r.pid})` : undefined;
  } catch {
    return undefined;
  } finally {
    db.close();
  }
}

export function sqliteSignature(): string {
  const parts: string[] = [];
  for (const f of ["master.sqlite", "master.sqlite-wal"]) {
    try {
      const st = statSync(join(sqliteDir(), f));
      parts.push(`${f}:${st.mtimeMs}:${st.size}`);
    } catch {
      parts.push(f + ":none");
    }
  }
  return parts.join("|");
}
