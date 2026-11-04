/**
 * Serato (and Mixed In Key) metadata stored inside audio files.
 *
 * MP3/AIFF/WAV: ID3 GEOB frames ("Serato Markers2", "Serato BeatGrid",
 * "Serato Autotags", plus Mixed In Key's base64-JSON "Key"/"Energy"/"CuePoints").
 * MP4: "----:com.serato.dj:*" atoms. FLAC/Ogg: SERATO_* vorbis comments.
 * The MP4/FLAC forms wrap the same payload in base64 with a small mime header.
 */
import { parseFile } from "music-metadata";

export interface CuePoint {
  index: number; // 0-based hot cue slot
  positionMs: number;
  color: string; // #rrggbb
  name: string;
}

export interface Loop {
  index: number;
  startMs: number;
  endMs: number;
  locked: boolean;
  name: string;
}

export interface BeatGridMarker {
  positionSec: number;
  bpm?: number; // terminal marker
  beatsUntilNext?: number; // non-terminal markers
}

export interface FileAnalysis {
  durationSec?: number;
  cues: CuePoint[];
  loops: Loop[];
  trackColor?: string;
  bpmLocked?: boolean;
  beatGrid: BeatGridMarker[];
  autoBpm?: number;
  autoGainDb?: number;
  mikKey?: string;
  mikEnergy?: number;
  tagBpm?: number;
  tagKey?: string;
  comment?: string;
}

const hex = (r: number, g: number, b: number) =>
  "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");

function readCString(buf: Buffer, pos: number): [string, number] {
  const end = buf.indexOf(0, pos);
  const stop = end === -1 ? buf.length : end;
  return [buf.toString("utf8", pos, stop), stop + 1];
}

function b64(text: string): Buffer {
  const clean = text.replace(/[^A-Za-z0-9+/]/g, "");
  // Serato sometimes writes base64 whose length is 1 mod 4; drop the stray char.
  const usable = clean.length % 4 === 1 ? clean.slice(0, -1) : clean;
  return Buffer.from(usable, "base64");
}

/** Markers2 GEOB payload: 0x01 0x01 then base64 of (0x01 0x01 + entries). */
export function parseMarkers2(payload: Buffer): Pick<FileAnalysis, "cues" | "loops" | "trackColor" | "bpmLocked"> {
  const out = { cues: [] as CuePoint[], loops: [] as Loop[], trackColor: undefined as string | undefined, bpmLocked: undefined as boolean | undefined };
  if (payload.length < 3) return out;
  const textEnd = payload.indexOf(0, 2);
  const decoded = b64(payload.toString("latin1", 2, textEnd === -1 ? payload.length : textEnd));
  let pos = 2; // skip inner version
  while (pos < decoded.length) {
    const [name, after] = readCString(decoded, pos);
    if (!name) break;
    if (after + 4 > decoded.length) break;
    const len = decoded.readUInt32BE(after);
    const data = decoded.subarray(after + 4, after + 4 + len);
    pos = after + 4 + len;
    if (name === "CUE" && data.length >= 13) {
      out.cues.push({
        index: data[1],
        positionMs: data.readUInt32BE(2),
        color: hex(data[7], data[8], data[9]),
        name: readCString(data, 12)[0],
      });
    } else if (name === "LOOP" && data.length >= 20) {
      out.loops.push({
        index: data[1],
        startMs: data.readUInt32BE(2),
        endMs: data.readUInt32BE(6),
        locked: data[19] !== 0,
        name: readCString(data, 20)[0],
      });
    } else if (name === "COLOR" && data.length >= 4) {
      out.trackColor = hex(data[1], data[2], data[3]);
    } else if (name === "BPMLOCK" && data.length >= 1) {
      out.bpmLocked = data[0] !== 0;
    }
  }
  out.cues.sort((a, b) => a.index - b.index);
  out.loops.sort((a, b) => a.index - b.index);
  return out;
}

export function parseBeatGrid(payload: Buffer): BeatGridMarker[] {
  if (payload.length < 6) return [];
  const count = payload.readUInt32BE(2);
  const markers: BeatGridMarker[] = [];
  let pos = 6;
  for (let i = 0; i < count && pos + 8 <= payload.length; i++, pos += 8) {
    const positionSec = payload.readFloatBE(pos);
    if (i === count - 1) markers.push({ positionSec, bpm: round(payload.readFloatBE(pos + 4), 3) });
    else markers.push({ positionSec, beatsUntilNext: payload.readUInt32BE(pos + 4) });
  }
  return markers;
}

export function parseAutotags(payload: Buffer): { autoBpm?: number; autoGainDb?: number } {
  const parts: string[] = [];
  let pos = 2;
  while (pos < payload.length) {
    const [s, next] = readCString(payload, pos);
    parts.push(s);
    pos = next;
  }
  const bpm = parseFloat(parts[0]);
  const gain = parseFloat(parts[2]);
  return { autoBpm: bpm > 0 ? bpm : undefined, autoGainDb: Number.isFinite(gain) ? gain : undefined };
}

/** MP4 / FLAC wrap payloads as base64("application/octet-stream\0\0<name>\0" + payload). */
function unwrapBase64Envelope(text: string): Buffer {
  const raw = b64(text);
  const prefix = "application/octet-stream";
  if (raw.toString("latin1", 0, prefix.length) !== prefix) return raw;
  let pos = prefix.length;
  while (raw[pos] === 0) pos++;
  pos = raw.indexOf(0, pos) + 1; // skip the name
  return raw.subarray(pos);
}

function parseMikJson(data: Buffer): Record<string, unknown> | undefined {
  try {
    return JSON.parse(b64(data.toString("latin1")).toString("utf8"));
  } catch {
    return undefined;
  }
}

const round = (n: number, d = 2) => Math.round(n * 10 ** d) / 10 ** d;

export async function readFileAnalysis(path: string): Promise<FileAnalysis> {
  const md = await parseFile(path, { skipCovers: true, duration: false });
  const res: FileAnalysis = { cues: [], loops: [], beatGrid: [] };
  res.durationSec = md.format.duration ? round(md.format.duration) : undefined;
  res.tagBpm = md.common.bpm;
  res.tagKey = md.common.key;
  res.comment = md.common.comment?.map((c) => (typeof c === "string" ? c : c.text)).filter(Boolean).join(" | ") || undefined;

  const geob = new Map<string, Buffer>();
  for (const [format, items] of Object.entries(md.native)) {
    for (const item of items) {
      const v = item.value as any;
      if (item.id === "GEOB" && v?.description) {
        geob.set(v.description, Buffer.from(v.data));
      } else if (format === "iTunes" && typeof item.id === "string" && item.id.startsWith("----:com.serato.dj:")) {
        const key = item.id.slice("----:com.serato.dj:".length).toLowerCase();
        const text = Buffer.isBuffer(v) || v instanceof Uint8Array ? Buffer.from(v).toString("latin1") : String(v);
        const map: Record<string, string> = { markersv2: "Serato Markers2", beatgrid: "Serato BeatGrid", autgain: "Serato Autotags" };
        if (map[key]) geob.set(map[key], key === "markersv2" ? Buffer.concat([Buffer.from([1, 1]), Buffer.from(text, "latin1")]) : unwrapBase64Envelope(text));
      } else if (format === "vorbis" && typeof item.id === "string" && item.id.startsWith("SERATO_")) {
        const map: Record<string, string> = { SERATO_MARKERS_V2: "Serato Markers2", SERATO_BEATGRID: "Serato BeatGrid", SERATO_AUTOTAGS: "Serato Autotags" };
        if (map[item.id]) geob.set(map[item.id], unwrapBase64Envelope(String(v)));
      }
    }
  }

  const m2 = geob.get("Serato Markers2");
  if (m2) Object.assign(res, parseMarkers2(m2));
  const bg = geob.get("Serato BeatGrid");
  if (bg) res.beatGrid = parseBeatGrid(bg);
  const at = geob.get("Serato Autotags");
  if (at) Object.assign(res, parseAutotags(at));

  const mikKey = geob.get("Key") && parseMikJson(geob.get("Key")!);
  if (mikKey && typeof mikKey.key === "string") res.mikKey = mikKey.key;
  const mikEnergy = geob.get("Energy") && parseMikJson(geob.get("Energy")!);
  if (mikEnergy && typeof mikEnergy.energyLevel === "number") res.mikEnergy = mikEnergy.energyLevel;
  return res;
}
