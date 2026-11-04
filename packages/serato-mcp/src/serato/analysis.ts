import { statSync } from "node:fs";
import { readFileAnalysis, type FileAnalysis } from "./tags.js";
import type { Track } from "./library.js";

const cache = new Map<string, { mtimeMs: number; analysis: FileAnalysis | null }>();

/** Cue points, beatgrid and Mixed In Key data read from the audio file (cached by mtime). */
export async function analyze(track: Track): Promise<FileAnalysis | null> {
  if (track.missing) return null;
  let mtimeMs: number;
  try {
    mtimeMs = statSync(track.path).mtimeMs;
  } catch {
    return null;
  }
  const hit = cache.get(track.path);
  if (hit && hit.mtimeMs === mtimeMs) return hit.analysis;
  let analysis: FileAnalysis | null = null;
  try {
    analysis = await readFileAnalysis(track.path);
  } catch (err) {
    process.stderr.write(`serato-mcp: could not read tags of ${track.path}: ${err}\n`);
  }
  cache.set(track.path, { mtimeMs, analysis });
  return analysis;
}

export async function analyzeMany(tracks: Track[], concurrency = 16): Promise<Map<string, FileAnalysis | null>> {
  const out = new Map<string, FileAnalysis | null>();
  let i = 0;
  const worker = async () => {
    while (i < tracks.length) {
      const t = tracks[i++];
      out.set(t.path, await analyze(t));
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, tracks.length) }, worker));
  return out;
}
