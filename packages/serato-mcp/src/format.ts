import { writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import type { Track } from "./serato/library.js";
import { mmss } from "./mix/planner.js";

/** Compact track shape for lists. */
export function brief(t: Track) {
  return {
    path: t.path,
    artist: t.artist,
    title: t.title,
    bpm: t.bpm,
    key: t.camelot ? `${t.camelot} (${t.keyName})` : t.key,
    energy: t.energy,
    genre: t.genre,
    length: t.durationSec ? mmss(t.durationSec) : undefined,
    ...(t.playCount ? { plays: t.playCount } : {}),
    ...(t.source && t.source !== "local" ? { source: t.source } : {}),
    ...(t.missing ? { missing: true } : {}),
    ...(t.crates.length ? { crates: t.crates } : {}),
  };
}

export function writeM3U(file: string, tracks: Track[]): string {
  const lines = ["#EXTM3U"];
  for (const t of tracks) {
    lines.push(`#EXTINF:${Math.round(t.durationSec ?? -1)},${t.artist ? t.artist + " - " : ""}${t.title ?? ""}`);
    lines.push(t.path);
  }
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, lines.join("\n") + "\n", "utf8");
  return file;
}

export function json(value: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }] };
}

export function fail(err: unknown) {
  return { content: [{ type: "text" as const, text: `Error: ${err instanceof Error ? err.message : String(err)}` }], isError: true };
}
