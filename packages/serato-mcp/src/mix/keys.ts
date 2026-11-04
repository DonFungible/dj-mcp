/**
 * Musical key parsing and harmonic compatibility on the Camelot wheel.
 * Accepts Serato/standard names (Gmin, F#m, Bb, Ebmaj, D#min), Camelot
 * (8A, 11B) and Open Key (1m, 6d).
 */

export interface Camelot {
  num: number; // 1..12
  letter: "A" | "B"; // A = minor, B = major
}

const PITCH: Record<string, number> = {
  C: 0, "B#": 0, "C#": 1, DB: 1, D: 2, "D#": 3, EB: 3, E: 4, FB: 4, F: 5, "E#": 5,
  "F#": 6, GB: 6, G: 7, "G#": 8, AB: 8, A: 9, "A#": 10, BB: 10, B: 11, CB: 11,
};

// Camelot number by pitch class (C=0) for minor and major keys.
const MINOR_CAMELOT = [5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10];
const MAJOR_CAMELOT = [8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1];

const MINOR_NAMES = ["Cm", "C#m", "Dm", "Ebm", "Em", "Fm", "F#m", "Gm", "Abm", "Am", "Bbm", "Bm"];
const MAJOR_NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"];

export function parseKey(input: string | undefined | null): Camelot | undefined {
  if (!input) return undefined;
  const s = input.trim().replace(/♯/g, "#").replace(/♭/g, "b");
  let m = /^(\d{1,2})\s*([AB])$/i.exec(s);
  if (m) {
    const num = Number(m[1]);
    if (num >= 1 && num <= 12) return { num, letter: m[2].toUpperCase() as "A" | "B" };
    return undefined;
  }
  m = /^(\d{1,2})\s*([md])$/i.exec(s); // Open Key: 1m = 8A, 1d = 8B
  if (m) {
    const n = Number(m[1]);
    if (n < 1 || n > 12) return undefined;
    return { num: ((n + 6) % 12) + 1, letter: m[2].toLowerCase() === "m" ? "A" : "B" };
  }
  m = /^([A-Ga-g])([#b]?)\s*(maj(or)?|min(or)?|m|M)?$/.exec(s);
  if (!m) return undefined;
  const pc = PITCH[m[1].toUpperCase() + (m[2] === "b" ? "B" : m[2])];
  if (pc === undefined) return undefined;
  const q = m[3] ?? "";
  const minor = /^min/i.test(q) || q === "m";
  return minor ? { num: MINOR_CAMELOT[pc], letter: "A" } : { num: MAJOR_CAMELOT[pc], letter: "B" };
}

export const camelotString = (c: Camelot) => `${c.num}${c.letter}`;

export function keyName(c: Camelot): string {
  const table = c.letter === "A" ? MINOR_CAMELOT : MAJOR_CAMELOT;
  const pc = table.indexOf(c.num);
  return (c.letter === "A" ? MINOR_NAMES : MAJOR_NAMES)[pc];
}

const wrap = (n: number) => ((((n - 1) % 12) + 12) % 12) + 1;

export interface KeyRelation {
  score: number; // 0..1
  relation: string;
  description: string;
}

/** How well track B's key follows track A's. */
export function keyCompatibility(a?: Camelot, b?: Camelot): KeyRelation {
  if (!a || !b) return { score: 0.5, relation: "unknown", description: "Key unknown for one of the tracks" };
  const step = (((b.num - a.num) % 12) + 12) % 12; // clockwise distance
  if (a.letter === b.letter) {
    if (step === 0) return { score: 1, relation: "same", description: "Same key: perfect harmonic match" };
    if (step === 1) return { score: 0.92, relation: "+1", description: "One step clockwise: smooth lift in energy" };
    if (step === 11) return { score: 0.9, relation: "-1", description: "One step counter-clockwise: smooth, slightly calmer" };
    if (step === 2) return { score: 0.62, relation: "+2", description: "Two steps clockwise: noticeable energy boost (whole-tone up)" };
    if (step === 7) return { score: 0.58, relation: "+7", description: "Semitone up (+7 on the wheel): classic energy-boost key change" };
    if (step === 10) return { score: 0.45, relation: "-2", description: "Two steps counter-clockwise: drops energy, usable in breakdowns" };
    if (step === 5) return { score: 0.35, relation: "-7", description: "Semitone down: energy drop, keep the overlap short" };
    return { score: 0.1, relation: "clash", description: "Keys clash: avoid long blends" };
  }
  if (step === 0)
    return { score: 0.88, relation: "relative", description: a.letter === "A" ? "Relative major: same notes, brighter mood" : "Relative minor: same notes, darker mood" };
  // Diagonal moves: nA -> (n+1)B and nB -> (n-1)A
  if ((a.letter === "A" && step === 1) || (a.letter === "B" && step === 11))
    return { score: 0.68, relation: "diagonal", description: "Diagonal move: mood change with a gentle lift" };
  if ((a.letter === "A" && step === 3) || (a.letter === "B" && step === 9))
    return { score: 0.4, relation: "parallel", description: "Parallel major/minor (same root): dramatic mood switch" };
  return { score: 0.1, relation: "clash", description: "Keys clash: avoid long blends" };
}

/** Keys that mix well out of `k`, best first. */
export function compatibleKeys(k: Camelot): { key: string; relation: string; score: number }[] {
  const all: Camelot[] = [];
  for (let n = 1; n <= 12; n++) for (const l of ["A", "B"] as const) all.push({ num: n, letter: l });
  return all
    .map((c) => ({ c, r: keyCompatibility(k, c) }))
    .filter((x) => x.r.score >= 0.55)
    .sort((x, y) => y.r.score - x.r.score)
    .map((x) => ({ key: camelotString(x.c), relation: x.r.relation, score: x.r.score }));
}

/** The key a track lands in when played `pct` faster without key lock. */
export function shiftedKey(k: Camelot, pct: number): Camelot {
  const semitones = Math.round(12 * Math.log2(1 + pct / 100));
  return { num: wrap(k.num + semitones * 7), letter: k.letter };
}
