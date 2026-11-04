// Read-only smoke test against the real Serato library. Runs with SERATO_MCP_READONLY=1 so nothing can be written.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
const client = new Client({ name: "live", version: "1" });
await client.connect(new StdioClientTransport({ command: "node", args: ["dist/index.js"], env: { ...process.env, SERATO_MCP_READONLY: "1" }, stderr: "inherit" }));
let failures = 0;
async function call(name, args = {}, show = 400, expectError = false) {
  const t0 = Date.now();
  const res = await client.callTool({ name, arguments: args });
  const text = res.content[0].text;
  const ok = expectError ? res.isError : !res.isError;
  if (!ok) failures++;
  console.log(`${ok ? "PASS" : "FAIL"} ${name} ${JSON.stringify(args).slice(0, 90)} (${Date.now() - t0}ms)\n   ${text.slice(0, show).replace(/\n\s*/g, " ")}`);
  return res.isError ? null : JSON.parse(text);
}
await call("serato_status", {}, 900);
const crates = (await call("list_crates", { include_playlists: false }, 500)) ?? [];
// Use whatever is in this library rather than names from one person's collection.
const crate = crates.find((c) => c.kind === "crate" && c.tracks > 1)?.name;
if (crate) await call("get_crate", { name: crate, limit: 2 });
await call("list_sessions", { limit: 3 });
await call("get_session", {}, 600);
await call("now_playing", {}, 500);
await call("play_stats", { limit: 3 }, 900);
const found = await call("search_tracks", { limit: 3 });
const someArtist = found?.tracks?.[0]?.artist ?? found?.[0]?.artist;
if (someArtist) await call("search_tracks", { query: someArtist, limit: 3 });
await call("search_tracks", { query: someArtist ?? "a", include_streaming: true, limit: 2 });
await call("library_stats", {}, 500);
await call("suggest_next_tracks", { limit: 3 }, 700);
const p = await call("plan_mix", { duration_min: 60, curve: "journey", exclude_played: true }, 300);
if (p) { console.log("  ", p.summary.keyPath, "\n  ", p.summary.energyPath, p.summary.estimatedLength, p.summary.averageTransitionScore); for (const t of p.setlist) console.log("   ", t["#"], t.at, t.bpm, t.key, t.artist, "-", t.title); }
const ps = await call("plan_mix", { count: 8, include_streaming: true, curve: "build" }, 200);
if (ps) for (const t of ps.setlist) console.log("   ", t["#"], t.bpm, t.key, t.source ?? "local", t.artist, "-", t.title);
if (crate) await call("analyze_set", { crate, reorder: true }, 600);
await call("create_crate", { name: "x", tracks: [someArtist ?? "a"] }, 200, true); // must be refused (read-only)
await client.close();
console.log(failures ? `${failures} FAILURES` : "ALL PASSED");
