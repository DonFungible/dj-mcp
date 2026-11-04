/**
 * Harmonic mix planning: transition scoring, next-track suggestions and full
 * set construction (beam search over the library against an energy curve).
 */
import { basename } from "node:path";
import type { FileAnalysis } from "../serato/tags.js";
import type { Track } from "../serato/library.js";
import { camelotString, keyCompatibility, parseKey, shiftedKey } from "./keys.js";

export interface MixTrack {
  track: Track;
  analysis?: FileAnalysis | null;
  energy?: number;
  energyEstimated: boolean;
}

/** Same recording regardless of file location: artist + title, falling back to the path. */
export function identity(t: Track): string {
  const n = (x: string) => x.toLowerCase().replace(/\(?(320kbps|clean|dirty)\)?/g, "").replace(/[^a-z0-9]+/g, "");
  return t.artist && t.title ? n(t.artist) + "|" + n(t.title) : t.path;
}

/** Drop duplicate copies, preferring files that exist, have cues, then the higher bitrate. */
export function dedupe(pool: MixTrack[]): MixTrack[] {
  const best = new Map<string, MixTrack>();
  const rank = (m: MixTrack) => (m.track.missing ? 0 : 100) + (m.analysis?.cues.length ? 10 : 0) + (parseFloat(m.track.bitrate ?? "0") || 0) / 1000;
  for (const m of pool) {
    const k = identity(m.track);
    const cur = best.get(k);
    if (!cur || rank(m) > rank(cur)) best.set(k, m);
  }
  return pool.filter((m) => best.get(identity(m.track)) === m);
}

export const label = (t: Track) => `${t.artist ? t.artist + " - " : ""}${t.title ?? basename(t.path)}`;

// ---- tempo ------------------------------------------------------------------

export interface TempoMatch {
  score: number;
  /** % the incoming track must be pitched to match the outgoing one. */
  pitchChangePct: number;
  /** How the incoming track's tempo relates to the outgoing one ("double-time" = incoming is ~2x). */
  mode: "direct" | "half-time" | "double-time" | "unknown";
}

export function tempoCompatibility(a?: number, b?: number): TempoMatch {
  if (!a || !b) return { score: 0.4, pitchChangePct: 0, mode: "unknown" };
  const options = [
    { mode: "direct" as const, target: a, penalty: 1 },
    { mode: "half-time" as const, target: a / 2, penalty: 0.8 },
    { mode: "double-time" as const, target: a * 2, penalty: 0.8 },
  ].map((o) => ({ ...o, pct: ((o.target - b) / b) * 100 }));
  const best = options.reduce((x, y) => (Math.abs(y.pct) * (2 - y.penalty) < Math.abs(x.pct) * (2 - x.penalty) ? y : x));
  const d = Math.abs(best.pct);
  const base = d <= 1 ? 1 : d <= 3 ? 1 - (d - 1) * 0.08 : d <= 6 ? 0.84 - (d - 3) * 0.15 : d <= 10 ? 0.39 - (d - 6) * 0.09 : 0;
  return { score: Math.max(0, base) * best.penalty, pitchChangePct: round(best.pct, 2), mode: best.mode };
}

// ---- energy -------------------------------------------------------------------

/** Energy 1..10: Mixed In Key value when present, otherwise estimated from BPM within the pool. */
export function withEnergy(tracks: Track[], analyses: Map<string, FileAnalysis | null>): MixTrack[] {
  const bpms = tracks.map((t) => t.bpm).filter((b): b is number => !!b).map(normBpm).sort((a, b) => a - b);
  const lo = bpms[Math.floor(bpms.length * 0.1)] ?? 110;
  const hi = bpms[Math.floor(bpms.length * 0.9)] ?? 130;
  return tracks.map((track) => {
    const analysis = analyses.get(track.path);
    const known = track.energy ?? analysis?.mikEnergy;
    if (known) return { track, analysis, energy: known, energyEstimated: false };
    if (!track.bpm) return { track, analysis, energy: undefined, energyEstimated: true };
    const x = hi > lo ? (normBpm(track.bpm) - lo) / (hi - lo) : 0.5;
    return { track, analysis, energy: round(3 + Math.min(1, Math.max(0, x)) * 5, 1), energyEstimated: true };
  });
}

/** Fold BPMs into a 70..140 window so a 174 DnB track compares as 87 half-time. */
const normBpm = (b: number) => (b > 140 ? b / 2 : b < 70 ? b * 2 : b);

export type CurveName = "warmup" | "build" | "peak" | "journey" | "steady" | "cooldown" | "rollercoaster";

export const CURVES: Record<CurveName, { description: string; fn: (t: number) => number }> = {
  warmup: { description: "Opening set: starts low (3) and eases up to 6 without peaking", fn: (t) => 3 + 3 * t },
  build: { description: "Steady climb from 4 to 9", fn: (t) => 4 + 5 * t },
  peak: { description: "Peak-time: holds 7-9 with a short breather two-thirds in", fn: (t) => 8 + Math.sin(t * Math.PI * 3) * 0.7 - (t > 0.6 && t < 0.72 ? 1.5 : 0) },
  journey: { description: "Classic arc: 4 → peak 9 around 70% → land at 6", fn: (t) => (t < 0.7 ? 4 + 5 * Math.sin((t / 0.7) * (Math.PI / 2)) : 9 - 3 * ((t - 0.7) / 0.3)) },
  steady: { description: "Flat energy, holds the starting level", fn: () => 6 },
  cooldown: { description: "Closing set: 8 down to 3", fn: (t) => 8 - 5 * t },
  rollercoaster: { description: "Waves between 5 and 9", fn: (t) => 7 + 2 * Math.sin(t * Math.PI * 4 - Math.PI / 2) },
};

// ---- mix points ---------------------------------------------------------------

export const barsToSec = (bars: number, bpm?: number) => (bpm ? (bars * 4 * 60) / bpm : bars * 2);

export function mixPoints(m: MixTrack): { mixInSec: number; mixOutSec: number; durationSec?: number; source: string } {
  const duration = m.analysis?.durationSec ?? m.track.durationSec;
  const cues = m.analysis?.cues ?? [];
  const bpm = m.track.bpm;
  const firstBeat = m.analysis?.beatGrid[0]?.positionSec;
  const sortedCues = [...cues].sort((a, b) => a.positionMs - b.positionMs);
  let mixIn = sortedCues.length ? sortedCues[0].positionMs / 1000 : firstBeat ?? 0;
  let mixOut = duration ? duration - barsToSec(32, bpm) : 0;
  let source = sortedCues.length ? "cue points" : firstBeat !== undefined ? "beatgrid" : "track length";
  if (duration && sortedCues.length >= 2) {
    // The last cue in the final 40% usually marks the outro.
    const last = sortedCues[sortedCues.length - 1].positionMs / 1000;
    if (last > duration * 0.6 && duration - last >= barsToSec(8, bpm)) mixOut = last;
  }
  if (duration && mixOut <= mixIn) {
    mixOut = Math.max(mixIn + 30, duration * 0.8);
    source += " (short track)";
  }
  return { mixInSec: round(mixIn, 1), mixOutSec: round(mixOut, 1), durationSec: duration ? round(duration, 1) : undefined, source };
}

// ---- transitions ---------------------------------------------------------------

export interface Transition {
  from: string;
  to: string;
  score: number; // 0..100
  key: { from?: string; to?: string; relation: string; description: string };
  tempo: { from?: number; to?: number; pitchChangePct: number; mode: string };
  energy: { from?: number; to?: number; delta?: number };
  technique: string;
  overlapBars: number;
  mixOutAt?: string; // mm:ss in the outgoing track
  mixInAt?: string; // mm:ss in the incoming track
  notes: string[];
}

export function transition(a: MixTrack, b: MixTrack): Transition {
  const ka = parseKey(a.track.camelot ?? a.track.key ?? a.analysis?.mikKey);
  const kb = parseKey(b.track.camelot ?? b.track.key ?? b.analysis?.mikKey);
  const key = keyCompatibility(ka, kb);
  const tempo = tempoCompatibility(a.track.bpm, b.track.bpm);
  const de = a.energy !== undefined && b.energy !== undefined ? round(b.energy - a.energy, 1) : undefined;
  const energyScore = de === undefined ? 0.6 : Math.max(0, 1 - Math.max(0, Math.abs(de) - 1) / 4);
  const score = Math.round((key.score * 0.45 + tempo.score * 0.4 + energyScore * 0.15) * 100);

  const notes: string[] = [];
  let technique: string;
  let overlapBars: number;
  const d = Math.abs(tempo.pitchChangePct);
  if (tempo.mode === "unknown") {
    technique = "Check BPM by ear: one of the tracks has no BPM analysed";
    overlapBars = 8;
  } else if (tempo.score === 0) {
    technique = "Echo-out / cut: tempos are too far apart to beatmatch. Echo or reverb out the last phrase and drop the next track on its first downbeat";
    overlapBars = 1;
  } else if (tempo.mode !== "direct") {
    technique = `${tempo.mode === "half-time" ? "Half" : "Double"}-time switch: bring the next track in over a breakdown so the groove change lands on a new phrase`;
    overlapBars = 8;
  } else if (key.score >= 0.85 && d <= 3) {
    technique = "Long blend: overlap 16-32 bars and swap the low EQ on a phrase change";
    overlapBars = 32;
  } else if (key.score >= 0.55 && d <= 6) {
    technique = "Standard blend: 16 bars with a bass swap at the drop";
    overlapBars = 16;
  } else if (key.score < 0.55 && d <= 6) {
    technique = "Short blend or filter transition: keys clash, so keep the overlap to drums only (4-8 bars) and high-pass the outgoing track";
    overlapBars = 4;
  } else {
    technique = "Tempo ride: loop the outro and pitch towards the next track's tempo before blending";
    overlapBars = 8;
  }
  if (d > 3 && tempo.mode === "direct" && kb) {
    const landed = shiftedKey(kb, tempo.pitchChangePct);
    if (camelotString(landed) !== camelotString(kb)) notes.push(`Turn key lock on: pitching ${tempo.pitchChangePct > 0 ? "+" : ""}${tempo.pitchChangePct}% without it moves the key from ${camelotString(kb)} to ${camelotString(landed)}`);
  }
  if (de !== undefined && de >= 3) notes.push(`Big energy jump (+${de}): drop it after a breakdown or build-up for impact`);
  if (de !== undefined && de <= -3) notes.push(`Energy drops ${de}: mix out during the outgoing track's breakdown to avoid a sag`);
  if (a.energyEstimated || b.energyEstimated) notes.push("Energy is estimated from BPM for at least one track");

  const pa = mixPoints(a);
  const pb = mixPoints(b);
  return {
    from: label(a.track),
    to: label(b.track),
    score,
    key: { from: ka && camelotString(ka), to: kb && camelotString(kb), relation: key.relation, description: key.description },
    tempo: { from: a.track.bpm, to: b.track.bpm, pitchChangePct: tempo.pitchChangePct, mode: tempo.mode },
    energy: { from: a.energy, to: b.energy, delta: de },
    technique,
    overlapBars,
    mixOutAt: pa.durationSec ? mmss(pa.mixOutSec) : undefined,
    mixInAt: mmss(pb.mixInSec),
    notes,
  };
}

// ---- next-track suggestions -----------------------------------------------------

export interface Suggestion {
  track: Track;
  score: number;
  transition: Transition;
}

export function suggestNext(
  current: MixTrack,
  pool: MixTrack[],
  opts: { limit?: number; energy?: "up" | "down" | "same" | "any"; exclude?: Set<string> } = {}
): Suggestion[] {
  const out: Suggestion[] = [];
  const self = identity(current.track);
  for (const c of dedupe(pool)) {
    if (identity(c.track) === self || opts.exclude?.has(c.track.path) || opts.exclude?.has(identity(c.track))) continue;
    const tr = transition(current, c);
    let s = tr.score;
    const de = tr.energy.delta;
    if (de !== undefined && opts.energy && opts.energy !== "any") {
      if (opts.energy === "up") s += de > 0 && de <= 2 ? 10 : de <= 0 ? -15 : 0;
      if (opts.energy === "down") s += de < 0 && de >= -2 ? 10 : de >= 0 ? -15 : 0;
      if (opts.energy === "same") s -= Math.abs(de) * 5;
    }
    if (sameArtist(current.track, c.track)) s -= 8;
    out.push({ track: c.track, score: Math.round(s), transition: tr });
  }
  return out.sort((a, b) => b.score - a.score).slice(0, opts.limit ?? 10);
}

// ---- full set planning -----------------------------------------------------------

export interface PlanOptions {
  count?: number;
  durationMin?: number;
  curve?: CurveName;
  customCurve?: number[]; // energy targets spread evenly over the set
  startBpm?: number;
  endBpm?: number;
  start?: MixTrack;
  end?: MixTrack;
  mustInclude?: MixTrack[];
  harmonic?: "strict" | "balanced" | "loose";
  maxBpmJumpPct?: number;
  artistGap?: number;
  beamWidth?: number;
}

export interface PlannedTrack {
  position: number;
  track: Track;
  energy?: number;
  energyEstimated: boolean;
  targetEnergy: number;
  startsAt: string; // mm:ss into the set
  playFrom: string;
  mixOutAt: string;
}

export interface MixPlan {
  summary: {
    tracks: number;
    estimatedLength: string;
    averageTransitionScore: number;
    weakestTransition?: { position: number; score: number; between: string };
    curve: string;
    bpmRange: [number, number] | null;
    keyPath: string;
    energyPath: string;
  };
  tracks: PlannedTrack[];
  transitions: Transition[];
  warnings: string[];
}

function curveAt(opts: PlanOptions, t: number): number {
  if (opts.customCurve?.length) {
    const pts = opts.customCurve;
    if (pts.length === 1) return pts[0];
    const x = t * (pts.length - 1);
    const i = Math.min(pts.length - 2, Math.floor(x));
    return pts[i] + (pts[i + 1] - pts[i]) * (x - i);
  }
  return CURVES[opts.curve ?? "journey"].fn(t);
}

const playLengthSec = (m: MixTrack) => {
  const p = mixPoints(m);
  return p.durationSec ? Math.max(60, p.mixOutSec - p.mixInSec) : 300;
};

interface BeamState {
  seq: MixTrack[];
  used: Set<string>;
  score: number;
  included: number;
}

/** Plan, then nudge the track count until the set lands close to duration_min. */
export function planMix(pool: MixTrack[], opts: PlanOptions): MixPlan {
  let plan = planOnce(pool, opts);
  if (!opts.durationMin || opts.count) return plan;
  const target = opts.durationMin * 60;
  let best = plan;
  for (let i = 0; i < 4; i++) {
    const length = parseClock(plan.summary.estimatedLength);
    const avg = length / plan.summary.tracks;
    const diff = target - length;
    if (Math.abs(diff) <= avg * 0.6) break;
    const count = Math.max(2, plan.summary.tracks + Math.round(diff / avg));
    if (count === plan.summary.tracks) break;
    plan = planOnce(pool, { ...opts, count });
    if (Math.abs(parseClock(plan.summary.estimatedLength) - target) < Math.abs(parseClock(best.summary.estimatedLength) - target)) best = plan;
  }
  return best;
}

const parseClock = (s: string) => s.split(":").map(Number).reduce((acc, n) => acc * 60 + n, 0);

function planOnce(pool: MixTrack[], opts: PlanOptions): MixPlan {
  const warnings: string[] = [];
  const forced = [opts.start, opts.end, ...(opts.mustInclude ?? [])].filter(Boolean) as MixTrack[];
  const forcedIds = new Set(forced.map((m) => identity(m.track)));
  const candidates = [
    ...forced.filter((m, i) => forced.findIndex((x) => identity(x.track) === identity(m.track)) === i && m.track.bpm),
    ...dedupe(pool.filter((m) => m.track.bpm && !forcedIds.has(identity(m.track)))),
  ];
  if (pool.length !== candidates.length) warnings.push(`${pool.length - candidates.length} tracks without BPM were left out`);
  if (candidates.length < 2) throw new Error("Need at least 2 analysed tracks (with BPM) to plan a mix");

  let count = opts.count;
  if (!count) {
    const avg = candidates.reduce((s, m) => s + playLengthSec(m), 0) / candidates.length;
    count = opts.durationMin ? Math.max(2, Math.round((opts.durationMin * 60) / avg)) : Math.min(12, candidates.length);
  }
  if (count > candidates.length) {
    warnings.push(`Only ${candidates.length} usable tracks, so the set has ${candidates.length} instead of ${count}`);
    count = candidates.length;
  }

  const wKey = { strict: 55, balanced: 38, loose: 20 }[opts.harmonic ?? "balanced"];
  const maxJump = opts.maxBpmJumpPct ?? 8;
  const artistGap = opts.artistGap ?? 3;
  const must = new Set((opts.mustInclude ?? []).map((m) => identity(m.track)));
  const width = opts.beamWidth ?? 48;
  const bpmTarget = (t: number) =>
    opts.startBpm && opts.endBpm ? opts.startBpm + (opts.endBpm - opts.startBpm) * t : opts.startBpm ?? opts.endBpm;

  const stepScore = (prev: MixTrack | undefined, c: MixTrack, i: number, seq: MixTrack[]): number => {
    const t = count! === 1 ? 0 : i / (count! - 1);
    const target = curveAt(opts, t);
    let s = 0;
    if (c.energy !== undefined) s += (1 - Math.min(1, Math.abs(c.energy - target) / 5)) * 25;
    else s += 10;
    const bt = bpmTarget(t);
    if (bt && c.track.bpm) {
      const tm = tempoCompatibility(bt, c.track.bpm);
      s += tm.score * 20;
    }
    if (must.has(identity(c.track))) s += 30;
    if (!prev) return s;
    const ka = parseKey(prev.track.camelot ?? prev.track.key);
    const kb = parseKey(c.track.camelot ?? c.track.key);
    s += keyCompatibility(ka, kb).score * wKey;
    const tempo = tempoCompatibility(prev.track.bpm, c.track.bpm);
    if (Math.abs(tempo.pitchChangePct) > maxJump) s -= 60;
    s += tempo.score * 32;
    if (prev.energy !== undefined && c.energy !== undefined) {
      const jump = Math.abs(c.energy - prev.energy);
      if (jump > 2) s -= (jump - 2) * 6;
    }
    for (let k = Math.max(0, seq.length - artistGap); k < seq.length; k++) if (sameArtist(seq[k].track, c.track)) s -= 18;
    if (prev.track.genre && prev.track.genre === c.track.genre) s += 4;
    return s;
  };

  const startPool = opts.start ? [opts.start] : candidates;
  let beam: BeamState[] = startPool
    .filter((m) => !opts.end || identity(m.track) !== identity(opts.end.track) || count === 1)
    .map((m) => ({ seq: [m], used: new Set([identity(m.track)]), score: stepScore(undefined, m, 0, []), included: must.has(identity(m.track)) ? 1 : 0 }))
    .sort((a, b) => b.score - a.score)
    .slice(0, width);

  for (let i = 1; i < count; i++) {
    const isLast = i === count - 1;
    const remainingSlots = count - i;
    const next: BeamState[] = [];
    for (const st of beam) {
      const prev = st.seq[st.seq.length - 1];
      const missingMust = [...must].filter((p) => !st.used.has(p)).length;
      let opts2 = isLast && opts.end ? [opts.end] : candidates.filter((m) => !st.used.has(identity(m.track)) && (!opts.end || identity(m.track) !== identity(opts.end.track)));
      // Reserve the remaining slots for required tracks once we run out of room.
      if (!(isLast && opts.end) && missingMust >= remainingSlots - (opts.end ? 1 : 0)) {
        const required = opts2.filter((m) => must.has(identity(m.track)));
        if (required.length) opts2 = required;
      }
      const scored = opts2.map((c) => ({ c, s: stepScore(prev, c, i, st.seq) })).sort((a, b) => b.s - a.s).slice(0, 14);
      for (const { c, s } of scored) {
        const used = new Set(st.used);
        used.add(identity(c.track));
        next.push({ seq: [...st.seq, c], used, score: st.score + s, included: st.included + (must.has(identity(c.track)) ? 1 : 0) });
      }
    }
    if (!next.length) break;
    // Keep diverse beams: dedupe states ending on the same track with the same set.
    const seen = new Set<string>();
    beam = next
      .sort((a, b) => b.score - a.score)
      .filter((s) => {
        const sig = s.seq[s.seq.length - 1].track.path + "|" + [...s.used].sort().join(",");
        if (seen.has(sig)) return false;
        seen.add(sig);
        return true;
      })
      .slice(0, width);
  }

  const best = beam.sort((a, b) => b.included - a.included || b.score - a.score)[0];
  const seq = best.seq;
  const missingMust = (opts.mustInclude ?? []).filter((m) => !best.used.has(identity(m.track)));
  if (missingMust.length) warnings.push(`Could not fit: ${missingMust.map((m) => label(m.track)).join(", ")}`);

  const transitions = seq.slice(1).map((m, i) => transition(seq[i], m));
  const tracks: PlannedTrack[] = [];
  let clock = 0;
  // The next track starts when the outgoing one reaches its mix-out point
  // (the start of its outro); the blend runs over that outro.
  seq.forEach((m, i) => {
    const p = mixPoints(m);
    const t = seq.length === 1 ? 0 : i / (seq.length - 1);
    tracks.push({
      position: i + 1,
      track: m.track,
      energy: m.energy,
      energyEstimated: m.energyEstimated,
      targetEnergy: round(curveAt(opts, t), 1),
      startsAt: mmss(clock),
      playFrom: mmss(p.mixInSec),
      mixOutAt: i === seq.length - 1 ? mmss(p.durationSec ?? p.mixOutSec) : mmss(p.mixOutSec),
    });
    const isLast = i === seq.length - 1;
    const end = isLast ? p.durationSec ?? p.mixOutSec : p.mixOutSec;
    clock += Math.max(30, end - p.mixInSec);
  });

  const scores = transitions.map((t) => t.score);
  const avg = scores.length ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : 100;
  const weakestIdx = scores.length ? scores.indexOf(Math.min(...scores)) : -1;
  for (const [i, t] of transitions.entries()) {
    if (t.key.relation === "clash") warnings.push(`Key clash at transition ${i + 1} → ${i + 2} (${t.key.from} → ${t.key.to}): ${t.technique.split(":")[0]}`);
    if (Math.abs(t.tempo.pitchChangePct) > maxJump && t.tempo.mode === "direct") warnings.push(`Large tempo jump at transition ${i + 1} → ${i + 2} (${t.tempo.from} → ${t.tempo.to} BPM)`);
  }
  const bpms = seq.map((m) => m.track.bpm!).filter(Boolean);
  return {
    summary: {
      tracks: seq.length,
      estimatedLength: mmss(clock),
      averageTransitionScore: avg,
      weakestTransition: weakestIdx >= 0 ? { position: weakestIdx + 1, score: scores[weakestIdx], between: `${transitions[weakestIdx].from} → ${transitions[weakestIdx].to}` } : undefined,
      curve: opts.customCurve ? `custom [${opts.customCurve.join(", ")}]` : `${opts.curve ?? "journey"}: ${CURVES[opts.curve ?? "journey"].description}`,
      bpmRange: bpms.length ? [Math.min(...bpms), Math.max(...bpms)] : null,
      keyPath: seq.map((m) => m.track.camelot ?? "?").join(" → "),
      energyPath: seq.map((m) => (m.energy === undefined ? "?" : `${m.energy}${m.energyEstimated ? "~" : ""}`)).join(" → "),
    },
    tracks,
    transitions,
    warnings,
  };
}

/** Score an existing ordering and suggest the best re-ordering of the same tracks. */
export function analyzeSet(seq: MixTrack[]) {
  const transitions = seq.slice(1).map((m, i) => transition(seq[i], m));
  const avg = transitions.length ? Math.round(transitions.reduce((s, t) => s + t.score, 0) / transitions.length) : 100;
  return { averageTransitionScore: avg, transitions };
}

export function reorderSet(seq: MixTrack[], opts: { keepFirst?: boolean; keepLast?: boolean; curve?: CurveName }): MixPlan {
  return planMix(seq, {
    count: seq.length,
    start: opts.keepFirst ? seq[0] : undefined,
    end: opts.keepLast ? seq[seq.length - 1] : undefined,
    mustInclude: seq,
    curve: opts.curve ?? "steady",
    beamWidth: 96,
  });
}

// ---- helpers ------------------------------------------------------------------

function artists(t: Track): string[] {
  return (t.artist ?? "")
    .toLowerCase()
    .split(/,|&| x | feat\.? | ft\.? | vs\.? /)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function sameArtist(a: Track, b: Track): boolean {
  const x = artists(a);
  return x.length > 0 && artists(b).some((n) => x.includes(n));
}

export function mmss(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${m}:${r}`;
}

const round = (n: number, d = 2) => Math.round(n * 10 ** d) / 10 ** d;
