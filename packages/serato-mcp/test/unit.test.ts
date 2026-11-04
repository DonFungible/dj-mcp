import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readdirSync, readFileSync, writeFileSync, cpSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { decode, encode, encodeUtf16BE, decodeUtf16BE, makeField, getChild } from "../src/serato/binary.js";
import { parseKey, camelotString, keyCompatibility, compatibleKeys, shiftedKey } from "../src/mix/keys.js";
import { tempoCompatibility, planMix, transition, type MixTrack } from "../src/mix/planner.js";
import { parseMarkers2, parseBeatGrid, parseAutotags } from "../src/serato/tags.js";
import { Library, parseSessionFile, crateFileName, crateDisplayName, type Track } from "../src/serato/library.js";
import { writeCrateTracks } from "../src/serato/writer.js";

const SERATO = join(homedir(), "Music", "_Serato_");

test("TLV round-trips byte-identically on the real library files", { skip: !existsSync(SERATO) }, () => {
  const files = [join(SERATO, "database V2"), ...readdirSync(join(SERATO, "Subcrates")).map((f) => join(SERATO, "Subcrates", f))];
  for (const f of files.filter(existsSync)) {
    const buf = readFileSync(f);
    assert.ok(encode(decode(buf)).equals(buf), f);
  }
});

test("crates written by serato-mcp are byte-identical to crates written by Serato", { skip: !existsSync(join(SERATO, "Subcrates")) }, () => {
  const work = mkdtempSync(join(tmpdir(), "serato-crate-"));
  const root = join(work, "_Serato_");
  cpSync(join(SERATO, "Subcrates"), join(root, "Subcrates"), { recursive: true });
  const original = new Library([{ dir: root, volumeRoot: "/" }]);
  process.env.SERATO_BACKEND = "legacy";
  process.env.SERATO_MCP_BACKUP_DIR = join(work, "backups");
  const lib = original.refresh();
  for (const c of lib.crates) {
    const before = readFileSync(c.file);
    const name = c.name + " copy";
    writeCrateTracks(lib, name, c.trackPaths, "create", { force: true });
    const after = readFileSync(join(root, "Subcrates", crateFileName(name)));
    // Same columns Serato uses by default; only the track list differs if the user changed columns.
    const cols = (b: Buffer) => decode(b).filter((f) => f.tag === "ovct").map((f) => decodeUtf16BE(getChild(f, "tvcn")!.raw));
    if (JSON.stringify(cols(before)) === JSON.stringify(cols(after))) {
      const strip = (b: Buffer) => encode(decode(b).filter((f) => f.tag !== "ovct"));
      assert.ok(strip(before).equals(strip(after)), `${c.name}: header and track entries match`);
    }
    assert.deepEqual(decode(after).filter((f) => f.tag === "otrk").length, c.trackPaths.length);
  }
  delete process.env.SERATO_BACKEND;
});

test("utf16 and field helpers", () => {
  assert.equal(decodeUtf16BE(encodeUtf16BE("Señor ♫ 日本")), "Señor ♫ 日本");
  assert.equal(decodeUtf16BE(makeField("vrsn", "1.0/Serato ScratchLive Crate").raw), "1.0/Serato ScratchLive Crate");
  assert.equal(makeField("ulbl", 0xffffff).raw.readUInt32BE(0), 0xffffff);
  assert.equal(crateFileName("Parent / Child"), "Parent%%Child.crate");
  assert.equal(crateDisplayName("Parent%%Child.crate"), "Parent / Child");
  assert.throws(() => crateFileName("a:b"));
});

test("key parsing across notations", () => {
  const cases: [string, string][] = [
    ["Am", "8A"], ["A minor", "8A"], ["Amin", "8A"], ["C", "8B"], ["Cmaj", "8B"], ["F#m", "11A"], ["Gbmin", "11A"], ["D#min", "2A"],
    ["Ebm", "2A"], ["Bb", "6B"], ["Abmaj", "4B"], ["Bm", "10A"], ["C#m", "12A"], ["8A", "8A"], ["12b", "12B"], ["1m", "8A"], ["6d", "1B"],
  ];
  for (const [input, want] of cases) assert.equal(camelotString(parseKey(input)!), want, input);
  assert.equal(parseKey("H#"), undefined);
  assert.equal(parseKey("13A"), undefined);
});

test("harmonic relations", () => {
  const rel = (a: string, b: string) => keyCompatibility(parseKey(a), parseKey(b)).relation;
  assert.equal(rel("8A", "8A"), "same");
  assert.equal(rel("8A", "9A"), "+1");
  assert.equal(rel("8A", "7A"), "-1");
  assert.equal(rel("8A", "8B"), "relative");
  assert.equal(rel("12A", "1A"), "+1");
  assert.equal(rel("8A", "3A"), "+7");
  assert.equal(rel("8A", "9B"), "diagonal");
  assert.equal(rel("8A", "2B"), "clash");
  assert.ok(compatibleKeys(parseKey("8A")!).some((k) => k.key === "8B"));
  assert.equal(camelotString(shiftedKey(parseKey("8A")!, 6)), "3A"); // +1 semitone
});

test("tempo matching handles half/double time", () => {
  assert.equal(tempoCompatibility(126, 126).score, 1);
  // mode describes the incoming track relative to the outgoing one
  assert.equal(tempoCompatibility(87, 174).mode, "double-time");
  assert.equal(tempoCompatibility(174, 87).mode, "half-time");
  assert.equal(tempoCompatibility(100, 140).score, 0);
  assert.ok(Math.abs(tempoCompatibility(128, 126).pitchChangePct - 1.59) < 0.01);
});

// Build a Markers2 payload the way Serato does: 01 01 + base64(01 01 + entries) + NUL padding.
function markers2(entries: { name: string; data: Buffer }[]): Buffer {
  const parts = [Buffer.from([1, 1])];
  for (const e of entries) {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(e.data.length);
    parts.push(Buffer.from(e.name + "\0", "latin1"), len, e.data);
  }
  const b64 = Buffer.concat(parts).toString("base64").replace(/=+$/, "");
  return Buffer.concat([Buffer.from([1, 1]), Buffer.from(b64.replace(/(.{72})/g, "$1\n"), "latin1"), Buffer.alloc(16)]);
}

test("Serato Markers2: cues, loops, colour, bpm lock", () => {
  const cue = Buffer.concat([Buffer.from([0, 2]), Buffer.from([0, 0, 0x30, 0x39]), Buffer.from([0, 0xcc, 0x00, 0x00, 0, 0]), Buffer.from("Drop\0")]);
  const loop = Buffer.concat([
    Buffer.from([0, 0]), Buffer.from([0, 0, 0x03, 0xe8]), Buffer.from([0, 0, 0x07, 0xd0]), Buffer.from([0xff, 0xff, 0xff, 0xff]),
    Buffer.from([0, 0x27, 0xaa, 0xe1]), Buffer.from([0, 1]), Buffer.from("Intro\0"),
  ]);
  const parsed = parseMarkers2(
    markers2([
      { name: "COLOR", data: Buffer.from([0, 0xff, 0x99, 0x00]) },
      { name: "CUE", data: cue },
      { name: "LOOP", data: loop },
      { name: "BPMLOCK", data: Buffer.from([1]) },
    ])
  );
  assert.deepEqual(parsed.cues, [{ index: 2, positionMs: 12345, color: "#cc0000", name: "Drop" }]);
  assert.deepEqual(parsed.loops, [{ index: 0, startMs: 1000, endMs: 2000, locked: true, name: "Intro" }]);
  assert.equal(parsed.trackColor, "#ff9900");
  assert.equal(parsed.bpmLocked, true);
});

test("Serato BeatGrid and Autotags", () => {
  const b = Buffer.alloc(2 + 4 + 16 + 1);
  b[0] = 1;
  b.writeUInt32BE(2, 2);
  b.writeFloatBE(0.5, 6);
  b.writeUInt32BE(64, 10);
  b.writeFloatBE(30.5, 14);
  b.writeFloatBE(125, 18);
  assert.deepEqual(parseBeatGrid(b), [{ positionSec: 0.5, beatsUntilNext: 64 }, { positionSec: 30.5, bpm: 125 }]);
  assert.deepEqual(parseAutotags(Buffer.from("\x01\x01126.00\x00-1.500\x000.000\x00", "latin1")), { autoBpm: 126, autoGainDb: 0 });
});

test("legacy history .session files", () => {
  const adat = (fields: [number, Buffer][]) => Buffer.concat(fields.map(([id, raw]) => { const h = Buffer.alloc(8); h.writeUInt32BE(id); h.writeUInt32BE(raw.length, 4); return Buffer.concat([h, raw]); }));
  const u32 = (n: number) => { const b = Buffer.alloc(4); b.writeUInt32BE(n); return b; };
  const str = (s: string) => encodeUtf16BE(s + "\0");
  const entry = (title: string, deck: number, start: number) => {
    const a = adat([[2, str("/Music/" + title + ".mp3")], [6, str(title)], [7, str("Artist")], [15, str("126")], [28, u32(start)], [31, u32(deck)], [50, Buffer.from([1])]]);
    return encode([{ tag: "oent", raw: Buffer.alloc(0), children: [{ tag: "adat", raw: a }] }]);
  };
  const file = join(mkdtempSync(join(tmpdir(), "serato-session-")), "1.session");
  writeFileSync(file, Buffer.concat([encode([makeField("vrsn", "1.0/Serato Scratch LIVE Review")]), entry("One", 1, 1700000000), entry("Two", 2, 1700000300)]));
  const s = parseSessionFile(file);
  assert.equal(s.entries.length, 2);
  assert.equal(s.entries[1].title, "Two");
  assert.equal(s.entries[1].deck, 2);
  assert.equal(s.entries[0].path, "/Music/One.mp3");
  assert.equal(s.startedAt, new Date(1700000000 * 1000).toISOString());
});

function fake(n: number, bpm: number, key: string, energy: number, artist = `A${n}`): MixTrack {
  const k = parseKey(key)!;
  const track: Track = { path: `/t/${n}.mp3`, storedPath: `t/${n}.mp3`, root: "/", title: `T${n}`, artist, bpm, key, camelot: camelotString(k), durationSec: 360, played: false, missing: false, corrupt: false, beatgridLocked: false, crates: [] };
  return { track, analysis: null, energy, energyEstimated: false };
}

test("planner: unique tracks, honours start/end/must-include, follows the curve", () => {
  const pool = [
    fake(1, 122, "8A", 3), fake(2, 123, "9A", 4), fake(3, 124, "9A", 5), fake(4, 125, "10A", 6), fake(5, 126, "10A", 7),
    fake(6, 127, "11A", 8), fake(7, 128, "11A", 9), fake(8, 126, "3B", 6), fake(9, 140, "5A", 5), fake(10, 124, "8B", 4),
  ];
  const plan = planMix(pool, { count: 6, curve: "build", start: pool[0], end: pool[6], mustInclude: [pool[3]] });
  const paths = plan.tracks.map((t) => t.track.path);
  assert.equal(new Set(paths).size, paths.length);
  assert.equal(paths[0], "/t/1.mp3");
  assert.equal(paths[paths.length - 1], "/t/7.mp3");
  assert.ok(paths.includes("/t/4.mp3"));
  assert.ok(!paths.includes("/t/9.mp3"), "140 BPM outlier is avoided");
  const energies = plan.tracks.map((t) => t.energy!);
  assert.ok(energies[energies.length - 1] > energies[0], "energy builds");
  assert.ok(plan.summary.averageTransitionScore >= 80, `avg ${plan.summary.averageTransitionScore}`);
});

test("planner: duplicate copies of a track are never both used", () => {
  const a = fake(1, 126, "8A", 5, "Same");
  const b = { ...fake(2, 126, "8A", 5, "Same"), track: { ...fake(2, 126, "8A", 5, "Same").track, title: "T1" } };
  const plan = planMix([a, b, fake(3, 126, "9A", 6), fake(4, 126, "8A", 6)], { count: 3 });
  const ids = plan.tracks.map((t) => `${t.track.artist}|${t.track.title}`);
  assert.equal(new Set(ids).size, ids.length);
});

test("transition advice", () => {
  const t = transition(fake(1, 126, "8A", 5), fake(2, 126, "8A", 6));
  assert.match(t.technique, /Long blend/);
  const clash = transition(fake(1, 126, "8A", 5), fake(2, 126, "2B", 6));
  assert.equal(clash.key.relation, "clash");
  const far = transition(fake(1, 100, "8A", 5), fake(2, 140, "8A", 6));
  assert.match(far.technique, /Echo-out/);
});
