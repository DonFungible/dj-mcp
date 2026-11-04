#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { homedir } from "node:os";
import { join } from "node:path";
import { Library, type Track } from "./serato/library.js";
import { analyze, analyzeMany } from "./serato/analysis.js";
import { sqliteLockOwner, masterPath } from "./serato/sqlite.js";
import { deleteCrate, editTracks, relocateMissing, removeFromCrate, renameCrate, seratoRunning, writeCrateTracks, backupBase } from "./serato/writer.js";
import { camelotString, compatibleKeys, keyCompatibility, keyName, parseKey } from "./mix/keys.js";
import { analyzeSet, CURVES, type CurveName, dedupe, identity, label, mixPoints, mmss, planMix, reorderSet, suggestNext, transition, withEnergy, type MixTrack, barsToSec } from "./mix/planner.js";
import { ACTIONS, bridge, mappingTable, messageFor, perform, PORT_NAME, sweep, tempoValue } from "./control/midi.js";
import * as app from "./control/app.js";
import { brief, fail, json, writeM3U } from "./format.js";

const lib = new Library();
const L = () => lib.refresh();

const server = new McpServer(
  { name: "serato-mcp", version: "1.0.0" },
  {
    instructions:
      "Tools for Serato DJ Pro: search the library, read cues/beatgrids, manage crates, read play history, plan harmonic mixes, " +
      "and control decks over a virtual MIDI device. Writes to the library refuse while Serato is running (quit it first, or pass force). " +
      "With Serato DJ 4, crate changes are written to the legacy _Serato_ files and imported by Serato on its next launch. " +
      "Deck control needs a one-time MIDI mapping: run midi_setup, then midi_learn for each action you want.",
  }
);

type Handler<T> = (args: T) => Promise<unknown> | unknown;
function tool<S extends z.ZodRawShape>(name: string, description: string, shape: S, handler: Handler<z.infer<z.ZodObject<S>>>, readOnly = true) {
  server.registerTool(
    name,
    { description, inputSchema: shape, annotations: { readOnlyHint: readOnly, destructiveHint: false, openWorldHint: false } },
    (async (args: z.infer<z.ZodObject<S>>) => {
      try {
        return json(await handler(args));
      } catch (err) {
        return fail(err);
      }
    }) as any
  );
}

// ---- shared helpers ----------------------------------------------------------

const trackRefs = z.array(z.string()).describe("Tracks as absolute paths or search text like 'artist - title'");
const force = z.boolean().optional().describe("Write even though Serato is running. Serato may overwrite or corrupt the change; avoid unless you know Serato is idle");

function resolveAll(refs: string[]): Track[] {
  return refs.map((r) => L().resolve(r));
}

async function asMix(tracks: Track[], pool: Track[] = tracks): Promise<MixTrack[]> {
  const all = [...new Set([...pool, ...tracks])];
  const an = await analyzeMany(all);
  const withE = withEnergy(all, an);
  const byPath = new Map(withE.map((m) => [m.track.path, m]));
  return tracks.map((t) => byPath.get(t.path)!);
}

interface PoolFilter {
  include_streaming?: boolean;
  crate?: string;
  genre?: string;
  include_missing?: boolean;
  bpm_min?: number;
  bpm_max?: number;
  exclude?: string[];
}

function poolTracks(f: PoolFilter): Track[] {
  const l = L();
  const playable = new Set(l.playable({ includeStreaming: f.include_streaming, includeMissing: f.include_missing }));
  let tracks = f.crate ? l.crate(f.crate).trackPaths.map((p) => l.track(p)).filter((t): t is Track => !!t && playable.has(t)) : [...playable];
  if (f.genre) {
    const g = f.genre.toLowerCase().split(",").map((s) => s.trim());
    tracks = tracks.filter((t) => t.genre && g.some((x) => t.genre!.toLowerCase().includes(x)));
  }
  if (f.bpm_min) tracks = tracks.filter((t) => t.bpm && t.bpm >= f.bpm_min!);
  if (f.bpm_max) tracks = tracks.filter((t) => t.bpm && t.bpm <= f.bpm_max!);
  if (f.exclude?.length) {
    const ex = new Set(resolveAll(f.exclude).map((t) => identity(t)));
    tracks = tracks.filter((t) => !ex.has(identity(t)));
  }
  return tracks;
}

const poolShape = {
  crate: z.string().optional().describe("Only use tracks from this crate"),
  genre: z.string().optional().describe("Only these genres (comma-separated, substring match)"),
  bpm_min: z.number().optional(),
  bpm_max: z.number().optional(),
  include_missing: z.boolean().optional().describe("Include tracks whose files are missing (default false)"),
  include_streaming: z.boolean().optional().describe("Include streaming tracks (Spotify, Tidal...) from Serato's library (default false)"),
  exclude: z.array(z.string()).optional().describe("Tracks to leave out (paths or search text)"),
};

function latestSession() {
  return L().sessions()[0];
}

// ---- library -------------------------------------------------------------------

tool("serato_status", "Overview: which Serato library is in use, whether Serato DJ is running, track/crate counts, history and MIDI status.", {}, () => {
  const l = L();
  const sessions = l.sessions();
  const sources: Record<string, number> = {};
  for (const t of l.tracks) sources[t.source ?? "local"] = (sources[t.source ?? "local"] ?? 0) + 1;
  const running = seratoRunning();
  return {
    seratoRunning: running,
    libraryLock: sqliteLockOwner(),
    writesAllowed: !running && process.env.SERATO_MCP_READONLY !== "1",
    backend: l.backend === "sqlite" ? `Serato DJ 4+ library (${masterPath()})` : "Legacy database V2 + crate files",
    legacyFolders: l.roots.map((r) => ({ folder: r.dir, drive: r.volumeRoot })),
    tracks: l.tracks.length,
    bySource: sources,
    playableLocalTracks: l.playable().length,
    missingFiles: l.tracks.filter((t) => t.missing).length,
    crates: l.crates.filter((c) => c.space === undefined || c.space === "Serato Library").length,
    playlists: l.crates.filter((c) => c.space && c.space !== "Serato Library").length,
    pendingImport: l.crates.filter((c) => c.pendingImport).map((c) => c.name),
    historySessions: sessions.length,
    lastSession: sessions[0] ? { name: sessions[0].name, startedAt: sessions[0].startedAt, endedAt: sessions[0].endedAt, tracks: sessions[0].entries.length } : null,
    midi: { port: PORT_NAME, actions: Object.keys(ACTIONS).length, ...bridge.ports() },
    backups: backupBase(),
  };
});

tool(
  "search_tracks",
  "Search and filter the Serato library by text, BPM, key, harmonic compatibility, energy, genre or crate.",
  {
    query: z.string().optional().describe("Text matched against artist, title, album, genre, label, remixer and file name"),
    bpm_min: z.number().optional(),
    bpm_max: z.number().optional(),
    key: z.string().optional().describe("Exact key in any notation: 8A, Am, A minor, 1m"),
    compatible_with_key: z.string().optional().describe("Only keys that mix harmonically with this key"),
    energy_min: z.number().optional(),
    energy_max: z.number().optional(),
    genre: z.string().optional(),
    crate: z.string().optional(),
    include_missing: z.boolean().optional(),
    include_streaming: z.boolean().optional().describe("Also search streaming tracks in Serato's library (default false)"),
    sort: z.enum(["relevance", "bpm", "key", "energy", "added", "artist", "title"]).optional(),
    limit: z.number().int().min(1).max(500).optional(),
  },
  (a) => {
    const l = L();
    let pool = l.playable({ includeStreaming: a.include_streaming, includeMissing: a.include_missing });
    if (a.crate) {
      const c = new Set(l.crate(a.crate).trackPaths);
      pool = pool.filter((t) => c.has(t.path));
    }
    let tracks = a.query ? l.search(a.query, 100000, pool) : [...pool];
    if (a.bpm_min) tracks = tracks.filter((t) => t.bpm && t.bpm >= a.bpm_min!);
    if (a.bpm_max) tracks = tracks.filter((t) => t.bpm && t.bpm <= a.bpm_max!);
    if (a.key) {
      const k = parseKey(a.key);
      if (!k) throw new Error(`Unrecognised key "${a.key}"`);
      tracks = tracks.filter((t) => t.camelot === camelotString(k));
    }
    if (a.compatible_with_key) {
      const k = parseKey(a.compatible_with_key);
      if (!k) throw new Error(`Unrecognised key "${a.compatible_with_key}"`);
      const ok = new Set(compatibleKeys(k).map((x) => x.key));
      tracks = tracks.filter((t) => t.camelot && ok.has(t.camelot));
    }
    if (a.energy_min) tracks = tracks.filter((t) => t.energy && t.energy >= a.energy_min!);
    if (a.energy_max) tracks = tracks.filter((t) => t.energy && t.energy <= a.energy_max!);
    if (a.genre) tracks = tracks.filter((t) => t.genre?.toLowerCase().includes(a.genre!.toLowerCase()));
    const cmp: Record<string, (x: Track, y: Track) => number> = {
      bpm: (x, y) => (x.bpm ?? 0) - (y.bpm ?? 0),
      key: (x, y) => (parseInt(x.camelot ?? "99") - parseInt(y.camelot ?? "99")) || (x.camelot ?? "").localeCompare(y.camelot ?? ""),
      energy: (x, y) => (y.energy ?? 0) - (x.energy ?? 0),
      added: (x, y) => (y.addedAt ?? "").localeCompare(x.addedAt ?? ""),
      artist: (x, y) => (x.artist ?? "").localeCompare(y.artist ?? ""),
      title: (x, y) => (x.title ?? "").localeCompare(y.title ?? ""),
    };
    if (a.sort && a.sort !== "relevance") tracks.sort(cmp[a.sort]);
    const limit = a.limit ?? 50;
    return { total: tracks.length, showing: Math.min(limit, tracks.length), tracks: tracks.slice(0, limit).map(brief) };
  }
);

tool(
  "get_track",
  "Full details for one track: all Serato metadata plus hot cues, saved loops, beatgrid, track colour and Mixed In Key data read from the file, mix-in/out points and compatible keys.",
  { track: z.string().describe("Absolute path or search text") },
  async (a) => {
    const t = L().resolve(a.track);
    const an = await analyze(t);
    const [m] = await asMix([t]);
    const k = parseKey(t.camelot ?? t.key ?? an?.mikKey);
    return {
      ...t,
      fileAnalysis: an
        ? {
            hotCues: an.cues.map((c) => ({ slot: c.index + 1, at: mmss(c.positionMs / 1000), ms: c.positionMs, name: c.name || undefined, color: c.color })),
            loops: an.loops.map((l) => ({ slot: l.index + 1, from: mmss(l.startMs / 1000), to: mmss(l.endMs / 1000), name: l.name || undefined, locked: l.locked })),
            beatGrid: an.beatGrid,
            trackColor: an.trackColor,
            bpmLocked: an.bpmLocked,
            serattoAutoBpm: an.autoBpm,
            autoGainDb: an.autoGainDb,
            mixedInKey: an.mikKey || an.mikEnergy ? { key: an.mikKey, energy: an.mikEnergy } : undefined,
          }
        : t.missing
          ? "File is missing; use relocate_missing_tracks"
          : "Could not read file tags",
      energy: m.energy,
      energyEstimated: m.energyEstimated,
      mixPoints: (() => {
        const p = mixPoints(m);
        return { playFrom: mmss(p.mixInSec), mixOutAt: mmss(p.mixOutSec), basedOn: p.source };
      })(),
      phrase: t.bpm ? { "8 bars": `${barsToSec(8, t.bpm).toFixed(1)}s`, "16 bars": `${barsToSec(16, t.bpm).toFixed(1)}s`, "32 bars": `${barsToSec(32, t.bpm).toFixed(1)}s` } : undefined,
      compatibleKeys: k ? compatibleKeys(k).map((x) => `${x.key} (${keyName(parseKey(x.key)!)}, ${x.relation})`) : undefined,
    };
  }
);

tool("library_stats", "Library breakdown by genre, BPM, key and energy, plus health checks (missing files, unanalysed tracks, duplicates).", { crate: z.string().optional() }, (a) => {
  const l = L();
  const tracks = a.crate ? l.crate(a.crate).trackPaths.map((p) => l.track(p)).filter((t): t is Track => !!t) : l.tracks;
  const count = (f: (t: Track) => string | undefined) => {
    const m = new Map<string, number>();
    for (const t of tracks) {
      const k = f(t) ?? "(none)";
      m.set(k, (m.get(k) ?? 0) + 1);
    }
    return Object.fromEntries([...m].sort((x, y) => y[1] - x[1]));
  };
  const ids = new Map<string, number>();
  for (const t of tracks) ids.set(identity(t), (ids.get(identity(t)) ?? 0) + 1);
  return {
    tracks: tracks.length,
    genres: count((t) => t.genre),
    bpm: count((t) => (t.bpm ? `${Math.floor(t.bpm / 5) * 5}-${Math.floor(t.bpm / 5) * 5 + 4}` : undefined)),
    keys: count((t) => t.camelot),
    energy: count((t) => (t.energy ? String(t.energy) : undefined)),
    health: {
      missingFiles: tracks.filter((t) => t.missing).length,
      noBpm: tracks.filter((t) => !t.bpm).length,
      noKey: tracks.filter((t) => !t.camelot).length,
      corrupt: tracks.filter((t) => t.corrupt).length,
      duplicateGroups: [...ids.values()].filter((n) => n > 1).length,
    },
  };
});

tool("find_duplicates", "List tracks that appear more than once in the library (same artist and title, different files).", {}, () => {
  const groups = new Map<string, Track[]>();
  for (const t of L().tracks) groups.set(identity(t), [...(groups.get(identity(t)) ?? []), t]);
  return [...groups.values()]
    .filter((g) => g.length > 1 && g[0].artist && g[0].title)
    .map((g) => ({ track: label(g[0]), copies: g.map((t) => ({ path: t.path, bitrate: t.bitrate, missing: t.missing, crates: t.crates })) }));
});

// ---- crates ----------------------------------------------------------------------

tool(
  "list_crates",
  "List crates, smart crates and (optionally) iTunes / streaming playlists with track counts. Nested crates are shown as 'Parent / Child'.",
  { include_playlists: z.boolean().optional().describe("Also list iTunes and streaming playlists (default true)") },
  (a) =>
    L()
      .crates.filter((c) => (a.include_playlists ?? true) || !c.space || c.space === "Serato Library")
      .filter((c) => c.kind !== "streaming folder" || c.trackPaths.length > 0)
      .map((c) => ({
        name: c.name,
        kind: c.kind ?? "crate",
        tracks: c.trackPaths.length,
        ...(c.pendingImport ? { pendingImport: true } : {}),
        ...(c.rules ? { rules: c.rules } : {}),
      }))
);

tool("get_crate", "Tracks in a crate, in crate order.", { name: z.string(), limit: z.number().int().optional() }, (a) => {
  const l = L();
  const c = l.crate(a.name);
  const tracks = c.trackPaths.map((p) => l.track(p) ?? ({ path: p, notInDatabase: true } as any));
  return {
    name: c.name,
    kind: c.kind ?? "crate",
    ...(c.pendingImport ? { pendingImport: true } : {}),
    ...(c.rules ? { rules: c.rules } : {}),
    count: tracks.length,
    tracks: tracks.slice(0, a.limit ?? 500).map((t: any) => (t.notInDatabase ? t : brief(t))),
  };
});

tool(
  "create_crate",
  "Create a crate (use 'Parent / Child' for a sub-crate), or replace/append its tracks. Serato must be closed.",
  { name: z.string(), tracks: trackRefs, mode: z.enum(["create", "replace", "append"]).optional().describe("Default create (fails if it exists)"), force },
  (a) => writeCrateTracks(L(), a.name, resolveAll(a.tracks).map((t) => t.path), a.mode ?? "create", { force: a.force }),
  false
);

tool("add_to_crate", "Add tracks to a crate (creates it if needed). Skips tracks already in it. Serato must be closed.", { name: z.string(), tracks: trackRefs, force }, (a) =>
  writeCrateTracks(L(), a.name, resolveAll(a.tracks).map((t) => t.path), "append", { force: a.force }), false
);

tool("remove_from_crate", "Remove tracks from a crate (the files and library entries stay). Serato must be closed.", { name: z.string(), tracks: trackRefs, force }, (a) =>
  removeFromCrate(L(), a.name, resolveAll(a.tracks).map((t) => t.path), { force: a.force }), false
);

tool("rename_crate", "Rename or move a crate (sub-crates move with it). Serato must be closed.", { name: z.string(), new_name: z.string(), force }, (a) =>
  renameCrate(L(), a.name, a.new_name, { force: a.force }), false
);

tool("delete_crate", "Delete a crate and its sub-crates. Tracks stay in the library; a backup copy is kept. Serato must be closed.", { name: z.string(), force }, (a) =>
  deleteCrate(L(), a.name, { force: a.force }), false
);

// ---- track metadata ------------------------------------------------------------------

tool(
  "update_tracks",
  "Edit Serato library metadata (title, artist, genre, comment, key, BPM, colour...) for one or more tracks. Changes go into Serato's database, not the audio file tags. Serato must be closed.",
  {
    edits: z
      .array(
        z.object({
          track: z.string(),
          title: z.string().optional(), artist: z.string().optional(), album: z.string().optional(), genre: z.string().optional(),
          comment: z.string().optional(), grouping: z.string().optional(), remixer: z.string().optional(), composer: z.string().optional(),
          label: z.string().optional(), year: z.string().optional(), key: z.string().optional(), bpm: z.string().optional(),
          color: z.string().optional().describe("Track colour as #rrggbb (#ffffff = none)"),
        })
      )
      .min(1),
    force,
  },
  (a) =>
    editTracks(
      L(),
      a.edits.map(({ track, ...changes }) => ({ path: L().resolve(track).path, changes })),
      { force: a.force }
    ),
  false
);

tool(
  "relocate_missing_tracks",
  "Find files Serato can't locate (moved or renamed) by searching folders, then repoint the library and every crate at them. Defaults to a dry run that only reports matches.",
  {
    search_dirs: z.array(z.string()).optional().describe("Folders to search (default: ~/Music)"),
    dry_run: z.boolean().optional().describe("Default true. Set false to apply"),
    force,
  },
  (a) => relocateMissing(L(), a.search_dirs ?? [join(homedir(), "Music")], { dryRun: a.dry_run ?? true, force: a.force }),
  false
);

// ---- history ---------------------------------------------------------------------------

tool("list_sessions", "Recent Serato play-history sessions (newest first).", { limit: z.number().int().optional() }, (a) =>
  L().sessions().slice(0, a.limit ?? 20).map((x) => ({ id: x.id, name: x.name, startedAt: x.startedAt, endedAt: x.endedAt, tracks: x.entries.length }))
);

tool("get_session", "Tracks played in a history session, in order (default: the latest).", { session: z.string().optional().describe("Session id or name") }, (a) => {
  const sessions = L().sessions();
  const s = a.session ? sessions.find((x) => String(x.id) === a.session || x.name === a.session || x.name.startsWith(a.session + " ")) : sessions[0];
  if (!s) throw new Error(sessions.length ? `No session "${a.session}". Use list_sessions.` : "No Serato history yet. It appears once tracks are played in Serato DJ.");
  return {
    id: s.id,
    name: s.name,
    startedAt: s.startedAt,
    endedAt: s.endedAt,
    tracks: s.entries.map((e, i) => ({ "#": i + 1, at: e.startTime, deck: e.deck, artist: e.artist, title: e.title, bpm: e.bpm, key: e.key, playedFor: e.playTimeSec !== undefined && e.playTimeSec < 7200 ? mmss(e.playTimeSec) : undefined, path: e.path })),
  };
});

tool("now_playing", "What is (or was last) loaded on each deck, from Serato's play history.", {}, () => {
  const s = latestSession();
  if (!s) return { playing: null, note: "No Serato history yet. It appears after tracks are played in Serato DJ." };
  const running = seratoRunning();
  const live = running && (!s.endedAt || Date.now() - Date.parse(s.modifiedAt) < 30 * 60 * 1000);
  const decks: Record<string, unknown> = {};
  for (const e of s.entries) if (e.deck) decks[`deck ${e.deck}`] = { artist: e.artist, title: e.title, bpm: e.bpm, key: e.key, loadedAt: e.startTime, path: e.path };
  const last = s.entries[s.entries.length - 1];
  return { session: s.name, live, seratoRunning: running, decks, lastLoaded: last ? `${last.artist ?? ""} - ${last.title ?? ""} (deck ${last.deck ?? "?"})` : null };
});

tool(
  "play_stats",
  "Your DJ habits from Serato: most-played tracks, recently played, and the transitions you play most often (track A followed by track B on another deck).",
  { limit: z.number().int().optional() },
  (a) => {
    const l = L();
    const n = a.limit ?? 15;
    const topPlayed = l.tracks.filter((t) => (t.playCount ?? 0) > 0).sort((x, y) => (y.playCount ?? 0) - (x.playCount ?? 0)).slice(0, n).map((t) => ({ plays: t.playCount, ...brief(t) }));
    const pairs = new Map<string, { from: string; to: string; times: number; sessions: Set<string> }>();
    const plays = new Map<string, number>();
    for (const s of l.sessions()) {
      const es = s.entries.filter((e) => e.title);
      for (const e of es) plays.set(`${e.artist ?? ""} - ${e.title}`, (plays.get(`${e.artist ?? ""} - ${e.title}`) ?? 0) + 1);
      for (let i = 1; i < es.length; i++) {
        if (es[i].deck && es[i - 1].deck && es[i].deck === es[i - 1].deck) continue; // same deck = not a mix
        const from = `${es[i - 1].artist ?? ""} - ${es[i - 1].title}`;
        const to = `${es[i].artist ?? ""} - ${es[i].title}`;
        if (from === to) continue;
        const k = from + "→" + to;
        const p = pairs.get(k) ?? { from, to, times: 0, sessions: new Set<string>() };
        p.times++;
        p.sessions.add(s.name);
        pairs.set(k, p);
      }
    }
    return {
      mostPlayed: topPlayed,
      mostPlayedInHistory: [...plays].sort((x, y) => y[1] - x[1]).slice(0, n).map(([track, times]) => ({ track, times })),
      favouriteTransitions: [...pairs.values()].sort((x, y) => y.times - x.times).slice(0, n).map((p) => ({ from: p.from, to: p.to, times: p.times, sessions: p.sessions.size })),
    };
  }
);

// ---- mixing ------------------------------------------------------------------------------

tool(
  "analyze_transition",
  "How well one track mixes into another: key relation, tempo change, energy change, recommended technique, mix-out/in points and warnings.",
  { from: z.string(), to: z.string() },
  async (a) => {
    const [x, y] = await asMix(resolveAll([a.from, a.to]));
    return transition(x, y);
  }
);

tool(
  "suggest_next_tracks",
  "Best tracks to play after a given track (or after what's on the deck now), ranked by harmonic, tempo and energy fit.",
  {
    track: z.string().optional().describe("Current track. Default: the last track in the live history"),
    energy: z.enum(["up", "down", "same", "any"]).optional().describe("Where to take the energy (default any)"),
    limit: z.number().int().optional(),
    exclude_played: z.boolean().optional().describe("Skip tracks already played in the latest session (default true)"),
    ...poolShape,
  },
  async (a) => {
    let ref = a.track;
    const s = latestSession();
    if (!ref) {
      const last = s?.entries[s.entries.length - 1];
      if (!last?.path && !last?.title) throw new Error("No track given and no history to work out what's playing");
      ref = last.path ?? `${last.artist ?? ""} ${last.title ?? ""}`;
    }
    const current = L().resolve(ref);
    const exclude = new Set<string>();
    if (a.exclude_played ?? true) for (const e of s?.entries ?? []) if (e.path) exclude.add(e.path);
    for (const e of s?.entries ?? []) if (e.path && L().track(e.path)) exclude.add(identity(L().track(e.path)!));
    const pool = poolTracks(a);
    const mix = await asMix(pool, pool);
    const [cur] = await asMix([current], pool);
    return {
      current: brief(current),
      suggestions: suggestNext(cur, mix, { limit: a.limit ?? 10, energy: a.energy, exclude }).map((x) => ({
        score: x.score,
        ...brief(x.track),
        key: brief(x.track).key ?? x.transition.key.to,
        how: `${x.transition.key.relation} key (${x.transition.key.from}→${x.transition.key.to}), ${x.transition.tempo.pitchChangePct >= 0 ? "+" : ""}${x.transition.tempo.pitchChangePct}% tempo, energy ${x.transition.energy.from ?? "?"}→${x.transition.energy.to ?? "?"}`,
        technique: x.transition.technique,
      })),
    };
  }
);

tool(
  "plan_mix",
  "Build a complete DJ set from the library: picks and orders tracks for harmonic flow, smooth tempo and a target energy curve, with timing, mix points and a technique for every transition. Optionally saves it as a crate and/or M3U playlist.",
  {
    duration_min: z.number().optional().describe("Target set length in minutes"),
    count: z.number().int().min(2).max(200).optional().describe("Number of tracks (overrides duration_min)"),
    curve: z.enum(Object.keys(CURVES) as [CurveName, ...CurveName[]]).optional().describe(Object.entries(CURVES).map(([k, v]) => `${k}: ${v.description}`).join("; ")),
    custom_curve: z.array(z.number().min(1).max(10)).optional().describe("Energy targets (1-10) spread across the set, e.g. [4,6,8,9,7]"),
    start_bpm: z.number().optional(),
    end_bpm: z.number().optional(),
    start_with: z.string().optional().describe("Opening track"),
    end_with: z.string().optional().describe("Closing track"),
    must_include: z.array(z.string()).optional(),
    harmonic: z.enum(["strict", "balanced", "loose"]).optional().describe("How much key compatibility matters (default balanced)"),
    max_bpm_jump_pct: z.number().optional().describe("Largest tempo change between tracks (default 8%)"),
    exclude_played: z.boolean().optional().describe("Leave out tracks played in the latest history session"),
    save_as_crate: z.string().optional().describe("Also save the set as this crate (Serato must be closed)"),
    export_m3u: z.string().optional().describe("Also write an .m3u8 playlist to this path"),
    force,
    ...poolShape,
  },
  async (a) => {
    const exclude = [...(a.exclude ?? [])];
    if (a.exclude_played) for (const e of latestSession()?.entries ?? []) if (e.path && L().track(e.path)) exclude.push(e.path);
    const pool = poolTracks({ ...a, exclude });
    const pins = resolveAll([...(a.start_with ? [a.start_with] : []), ...(a.end_with ? [a.end_with] : []), ...(a.must_include ?? [])]);
    const mix = await asMix([...new Set([...pool, ...pins])]);
    const find = (t: Track) => mix.find((m) => m.track.path === t.path)!;
    let i = 0;
    const start = a.start_with ? find(pins[i++]) : undefined;
    const end = a.end_with ? find(pins[i++]) : undefined;
    const must = pins.slice(i).map(find);
    const plan = planMix(mix, {
      count: a.count,
      durationMin: a.duration_min,
      curve: a.curve,
      customCurve: a.custom_curve,
      startBpm: a.start_bpm,
      endBpm: a.end_bpm,
      start,
      end,
      mustInclude: must,
      harmonic: a.harmonic,
      maxBpmJumpPct: a.max_bpm_jump_pct,
    });
    const out: Record<string, unknown> = {
      summary: plan.summary,
      warnings: plan.warnings,
      setlist: plan.tracks.map((p, idx) => ({
        "#": p.position,
        at: p.startsAt,
        ...brief(p.track),
        energy: p.energy === undefined ? undefined : `${p.energy}${p.energyEstimated ? " (est.)" : ""} / target ${p.targetEnergy}`,
        playFrom: p.playFrom,
        mixOutAt: p.mixOutAt,
        nextTransition: plan.transitions[idx]
          ? `${plan.transitions[idx].score}/100 · ${plan.transitions[idx].key.relation} key · ${plan.transitions[idx].tempo.pitchChangePct >= 0 ? "+" : ""}${plan.transitions[idx].tempo.pitchChangePct}% · ${plan.transitions[idx].technique}`
          : undefined,
      })),
      transitions: plan.transitions,
    };
    if (a.save_as_crate) out.crate = writeCrateTracks(L(), a.save_as_crate, plan.tracks.map((p) => p.track.path), "replace", { force: a.force });
    if (a.export_m3u) out.playlist = writeM3U(a.export_m3u, plan.tracks.map((p) => p.track));
    return out;
  }
);

tool(
  "analyze_set",
  "Score a setlist you already have, transition by transition, and optionally propose a better order of the same tracks.",
  {
    tracks: trackRefs.optional(),
    crate: z.string().optional().describe("Use a crate's order as the setlist"),
    reorder: z.boolean().optional().describe("Also suggest an improved order"),
    keep_first: z.boolean().optional(),
    keep_last: z.boolean().optional(),
  },
  async (a) => {
    const tracks = a.tracks?.length ? resolveAll(a.tracks) : a.crate ? L().crate(a.crate).trackPaths.map((p) => L().track(p)).filter((t): t is Track => !!t) : [];
    if (tracks.length < 2) throw new Error("Give at least 2 tracks or a crate");
    const mix = await asMix(tracks);
    const current = analyzeSet(mix);
    const out: Record<string, unknown> = {
      current: { averageScore: current.averageTransitionScore, transitions: current.transitions.map((t, i) => ({ "#": `${i + 1}→${i + 2}`, score: t.score, from: t.from, to: t.to, key: `${t.key.from}→${t.key.to} (${t.key.relation})`, tempo: `${t.tempo.pitchChangePct}%`, technique: t.technique })) },
    };
    if (a.reorder) {
      const better = reorderSet(dedupe(mix), { keepFirst: a.keep_first, keepLast: a.keep_last });
      out.suggestedOrder = {
        averageScore: better.summary.averageTransitionScore,
        keyPath: better.summary.keyPath,
        tracks: better.tracks.map((p) => label(p.track)),
        paths: better.tracks.map((p) => p.track.path),
      };
    }
    return out;
  }
);

tool("compatible_keys", "Keys that mix harmonically with a key (any notation), using the Camelot wheel.", { key: z.string() }, (a) => {
  const k = parseKey(a.key);
  if (!k) throw new Error(`Unrecognised key "${a.key}"`);
  return { key: `${camelotString(k)} (${keyName(k)})`, compatible: compatibleKeys(k).map((x) => ({ ...x, name: keyName(parseKey(x.key)!), why: keyCompatibility(k, parseKey(x.key)).description })) };
});

tool(
  "export_playlist",
  "Write tracks (or a crate) to an .m3u8 playlist file, e.g. to share a set or import it elsewhere.",
  { path: z.string().describe("Output file, e.g. ~/Desktop/set.m3u8"), tracks: trackRefs.optional(), crate: z.string().optional() },
  (a) => {
    const tracks = a.tracks?.length ? resolveAll(a.tracks) : a.crate ? L().crate(a.crate).trackPaths.map((p) => L().track(p)).filter((t): t is Track => !!t) : [];
    if (!tracks.length) throw new Error("Give tracks or a crate");
    return { file: writeM3U(a.path.replace(/^~(?=\/)/, homedir()), tracks), tracks: tracks.length };
  },
  false
);

// ---- MIDI deck control ------------------------------------------------------------------------

const deck = z.number().int().min(1).max(4);
const actionName = z.enum(Object.keys(ACTIONS) as [string, ...string[]]);

tool(
  "midi_setup",
  "Open the 'Serato MCP' virtual MIDI device and get the one-time steps to map it in Serato DJ Pro.",
  {},
  () => {
    const port = bridge.open();
    return {
      ...port,
      steps: [
        `In Serato DJ Pro open Setup > MIDI and make sure "${PORT_NAME}" is listed and enabled (restart Serato if it isn't there yet; this MCP server must be running).`,
        "Click the MIDI button at the top of Serato to enter MIDI assign mode.",
        "Click the on-screen control you want to map (e.g. deck 1 play), then call midi_learn with that action and deck. Serato binds the message it receives.",
        "Repeat for each control you want, then leave assign mode and save the preset in Setup > MIDI.",
        "From then on use deck_control, set_tempo, fader_sweep and auto_transition.",
      ],
      tip: "Mapping order that covers most needs: play_pause, cue, sync, load_selected, volume, eq_low, filter, tempo for decks 1 and 2, then crossfader.",
      ports: bridge.ports(),
    };
  },
  false
);

tool("midi_mapping", "The full table of MIDI messages serato-mcp sends for each action.", {}, () => mappingTable());

tool(
  "midi_learn",
  "Send one action's MIDI message (optionally repeated) so Serato can learn it in MIDI assign mode. Click the target control in Serato first.",
  {
    action: actionName,
    deck: deck.optional(),
    repeat: z.number().int().min(1).max(20).optional(),
    interval_ms: z.number().int().min(100).max(5000).optional(),
  },
  async (a) => {
    bridge.open();
    let msg;
    for (let i = 0; i < (a.repeat ?? 1); i++) {
      msg = ACTIONS[a.action].kind === "fader" ? (await sweep(a.action, a.deck, 0, 127, 300), messageFor(a.action, a.deck, 127)) : await perform(a.action, a.deck);
      if (i < (a.repeat ?? 1) - 1) await new Promise((r) => setTimeout(r, a.interval_ms ?? 1000));
    }
    return { sent: msg, mapTo: ACTIONS[a.action].serato };
  },
  false
);

tool(
  "deck_control",
  "Press a deck/mixer control or set a fader through MIDI: play_pause, cue, sync, hotcue_1..8, loops, key lock, EQ, filter, volume, crossfader, library browsing, sampler and more (see midi_mapping). Requires the action to be mapped in Serato.",
  {
    action: actionName,
    deck: deck.optional().describe("1-4 for deck actions; omit for global ones (crossfader, library, sampler)"),
    value: z.number().min(0).max(127).optional().describe("Fader value 0-127 (64 = centre)"),
    hold_ms: z.number().int().optional().describe("How long to hold a button"),
  },
  async (a) => ({ sent: await perform(a.action, a.deck, a.value, a.hold_ms) }),
  false
);

tool(
  "deck_sequence",
  "Run several deck actions in order with waits between them, e.g. load, sync, play, then a filter sweep. Each step is an action with optional deck/value, or a wait.",
  {
    steps: z
      .array(
        z.object({
          action: z.string().optional().describe("Action name, or omit for a pure wait"),
          deck: deck.optional(),
          value: z.number().min(0).max(127).optional(),
          to_value: z.number().min(0).max(127).optional().describe("Sweep a fader from value to to_value over duration_ms"),
          duration_ms: z.number().int().optional(),
          wait_ms: z.number().int().optional().describe("Pause after this step"),
        })
      )
      .min(1)
      .max(100),
  },
  async (a) => {
    const done: string[] = [];
    for (const s of a.steps) {
      if (s.action) {
        if (!ACTIONS[s.action]) throw new Error(`Unknown action ${s.action}`);
        if (s.to_value !== undefined) {
          await sweep(s.action, s.deck, s.value ?? 64, s.to_value, s.duration_ms ?? 1000);
          done.push(`${s.action}${s.deck ? ` deck ${s.deck}` : ""} ${s.value ?? 64}→${s.to_value}`);
        } else {
          await perform(s.action, s.deck, s.value);
          done.push(`${s.action}${s.deck ? ` deck ${s.deck}` : ""}${s.value !== undefined ? ` = ${s.value}` : ""}`);
        }
      }
      const wait = s.wait_ms;
      if (wait) await new Promise((r) => setTimeout(r, Math.min(wait, 120000)));
    }
    return { done };
  },
  false
);

tool(
  "set_tempo",
  "Set a deck's tempo fader to a pitch percentage (needs 'tempo' mapped). Match pitch_range to the deck's range in Serato (8, 16 or 50).",
  { deck, pitch_pct: z.number(), pitch_range: z.number().optional() },
  async (a) => {
    const v = tempoValue(a.pitch_pct, a.pitch_range ?? 8);
    await perform("tempo", a.deck, v);
    return { deck: a.deck, pitchPct: a.pitch_pct, faderValue: v, resolution: `${((2 * (a.pitch_range ?? 8)) / 127).toFixed(2)}% per step` };
  },
  false
);

tool(
  "fader_sweep",
  "Move a fader smoothly over time: a crossfade, filter sweep, volume fade or EQ swap.",
  { action: actionName, deck: deck.optional(), from: z.number().min(0).max(127), to: z.number().min(0).max(127), duration_ms: z.number().int().min(50).max(300000) },
  async (a) => ({ steps: await sweep(a.action, a.deck, a.from, a.to, a.duration_ms) }),
  false
);

tool(
  "auto_transition",
  "Perform a full blend from one deck to another over N bars: syncs and starts the incoming deck, fades it in, swaps the bass halfway, then fades the outgoing deck out. Needs play_pause, sync, volume and eq_low mapped for both decks. The tracks must already be loaded and cued.",
  {
    from_deck: deck,
    to_deck: deck,
    bars: z.number().int().min(1).max(64).optional().describe("Length of the blend (default 16)"),
    bpm: z.number().optional().describe("Tempo to time the bars (default: the incoming track's BPM from history, else 126)"),
    start_incoming: z.boolean().optional().describe("Press sync + play on the incoming deck first (default true)"),
    stop_outgoing: z.boolean().optional().describe("Pause the outgoing deck at the end (default true)"),
  },
  async (a) => {
    if (a.from_deck === a.to_deck) throw new Error("from_deck and to_deck must differ");
    const bars = a.bars ?? 16;
    let bpm = a.bpm;
    if (!bpm) {
      const e = latestSession()?.entries.filter((x) => x.deck === a.to_deck).pop();
      bpm = e?.bpm ? parseFloat(e.bpm) : 126;
    }
    const total = barsToSec(bars, bpm) * 1000;
    const steps: string[] = [];
    // Prepare incoming: silent, bass cut.
    await perform("volume", a.to_deck, 0);
    await perform("eq_low", a.to_deck, 0);
    if (a.start_incoming ?? true) {
      await perform("sync", a.to_deck);
      await perform("play_pause", a.to_deck);
      steps.push(`deck ${a.to_deck}: sync + play`);
    }
    await sweep("volume", a.to_deck, 0, 127, total * 0.4);
    steps.push(`deck ${a.to_deck}: volume up over ${(total * 0.4 / 1000).toFixed(1)}s`);
    // Bass swap on the halfway phrase.
    await Promise.all([sweep("eq_low", a.from_deck, 64, 0, Math.min(2000, total * 0.1)), sweep("eq_low", a.to_deck, 0, 64, Math.min(2000, total * 0.1))]);
    steps.push("bass swap");
    await new Promise((r) => setTimeout(r, total * 0.1));
    await sweep("volume", a.from_deck, 127, 0, total * 0.4);
    steps.push(`deck ${a.from_deck}: volume down`);
    if (a.stop_outgoing ?? true) {
      await perform("play_pause", a.from_deck);
      await perform("eq_low", a.from_deck, 64);
      steps.push(`deck ${a.from_deck}: paused, EQ reset`);
    }
    return { bars, bpm, seconds: +(total / 1000).toFixed(1), steps };
  },
  false
);

tool("midi_monitor", "MIDI messages Serato has sent back to the 'Serato MCP' device (LED feedback for mapped controls), newest last.", { limit: z.number().int().optional() }, (a) => {
  bridge.open();
  return { received: bridge.received(a.limit ?? 50) };
});

// ---- app control ------------------------------------------------------------------------------

tool("launch_serato", "Open Serato DJ Pro.", {}, () => app.launch(), false);
tool("quit_serato", "Quit Serato DJ Pro cleanly (it saves its library). Needed before crate or metadata edits.", {}, () => app.quit(), false);

tool(
  "load_track",
  "Load a library track onto a deck: types it into Serato's search box, then loads the top result with Shift+Left/Right (decks 1-2) or the MIDI load_selected action (any deck). Needs macOS Accessibility permission.",
  { track: z.string(), deck, method: z.enum(["keyboard", "midi"]).optional().describe("Default keyboard for decks 1-2, midi for 3-4") },
  async (a) => {
    const t = L().resolve(a.track);
    if (t.missing) throw new Error(`The file for "${label(t)}" is missing, so Serato can't load it. Run relocate_missing_tracks.`);
    const query = [t.artist, t.title].filter(Boolean).join(" ") || t.path.split("/").pop()!.replace(/\.[^.]+$/, "");
    await app.search(query);
    const method = a.method ?? (a.deck <= 2 ? "keyboard" : "midi");
    if (method === "keyboard") {
      if (a.deck > 2) throw new Error("Keyboard loading only reaches decks 1 and 2; use method midi");
      await app.loadSelected(a.deck as 1 | 2);
    } else await perform("load_selected", a.deck);
    return { loaded: label(t), deck: a.deck, searchedFor: query, method, note: "If another track loaded, the search had several matches; refine with search_tracks and try a more specific track." };
  },
  false
);

tool("serato_search_box", "Type text into Serato's on-screen library search (Cmd+F).", { query: z.string() }, async (a) => {
  await app.search(a.query);
  return { searched: a.query };
}, false);

tool(
  "send_keystroke",
  "Send a keyboard shortcut to Serato DJ Pro, e.g. key 'left' with ['shift'] to load deck 1. Keys: letters, digits, left/right/up/down, return, tab, escape, space, delete.",
  { key: z.string(), modifiers: z.array(z.enum(["command", "shift", "option", "control"])).optional() },
  async (a) => {
    await app.keystroke(a.key, a.modifiers ?? []);
    return { sent: [...(a.modifiers ?? []), a.key].join("+") };
  },
  false
);

// ---- prompts --------------------------------------------------------------------------------------

server.registerPrompt(
  "plan_set",
  {
    description: "Plan a DJ set from your Serato library",
    argsSchema: { length_minutes: z.string().optional(), vibe: z.string().optional(), crate: z.string().optional() },
  },
  (a) => ({
    messages: [
      {
        role: "user",
        content: {
          type: "text",
          text: `Plan a ${a.length_minutes ?? "60"}-minute DJ set${a.crate ? ` from my "${a.crate}" crate` : ""}${a.vibe ? ` with this vibe: ${a.vibe}` : ""}. ` +
            "Check serato_status and library_stats first, pick a fitting energy curve, call plan_mix, then walk me through the set: the arc, the standout transitions, and any risky ones with how to handle them. Offer to save it as a crate.",
        },
      },
    ],
  })
);

server.registerPrompt(
  "whats_next",
  { description: "Suggest what to play next based on what's on the deck" },
  () => ({
    messages: [{ role: "user", content: { type: "text", text: "Use now_playing and suggest_next_tracks to tell me the best 5 tracks to play next, with how to mix into each one." } }],
  })
);

// ---- start ------------------------------------------------------------------------------------------

const transport = new StdioServerTransport();
await server.connect(transport);
// Keep the virtual MIDI device present while the server runs so Serato's saved mapping applies.
if (process.env.SERATO_MCP_MIDI !== "off") {
  try {
    bridge.open();
  } catch (err) {
    process.stderr.write(`serato-mcp: MIDI unavailable: ${err}\n`);
  }
}
process.stderr.write(`serato-mcp running (${lib.roots.map((r) => r.dir).join(", ") || "no Serato library found"})\n`);
const shutdown = () => {
  bridge.close();
  process.exit(0);
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
