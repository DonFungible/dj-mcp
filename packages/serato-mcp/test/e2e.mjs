// End-to-end: start the server over stdio against a copy of a Serato library and call its tools.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { cpSync, mkdtempSync, existsSync, readFileSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

// BACKEND=legacy (database V2 + crate files) or BACKEND=sqlite (Serato DJ 4 library snapshot)
const backend = process.env.BACKEND ?? "legacy";

const source = process.env.SOURCE_SERATO ?? join(homedir(), "Music", "_Serato_");
const work = mkdtempSync(join(process.env.TMPDIR ?? tmpdir(), "serato-mcp-e2e-"));
const root = join(work, "_Serato_");
for (const f of ["database V2", "Subcrates", "neworder.pref"]) if (existsSync(join(source, f))) cpSync(join(source, f), join(root, f), { recursive: true });

const env = { ...process.env, SERATO_ROOTS: root, SERATO_MCP_BACKUP_DIR: join(work, "backups"), SERATO_BACKEND: backend };
if (backend === "sqlite") {
  const live = join(homedir(), "Library", "Application Support", "Serato", "Library", "master.sqlite");
  const snap = join(work, "sqlite");
  cpSync(join(live, ".."), snap, { recursive: true, filter: (f) => !/master\.sqlite/.test(f) && !/Backups/.test(f) });
  new DatabaseSync(live, { readOnly: true }).exec(`VACUUM INTO '${join(snap, "master.sqlite").replace(/'/g, "''")}'`);
  env.SERATO_SQLITE_DIR = snap;
}
console.log("backend:", backend);
const transport = new StdioClientTransport({
  command: "node",
  args: ["dist/index.js"],
  env,
  stderr: "inherit",
});
const client = new Client({ name: "e2e", version: "1" });
await client.connect(transport);

let failures = 0;
async function call(name, args = {}, { expectError = false, show = 300 } = {}) {
  const res = await client.callTool({ name, arguments: args });
  const text = res.content[0].text;
  const ok = expectError ? res.isError : !res.isError;
  if (!ok) failures++;
  console.log(`${ok ? "PASS" : "FAIL"} ${name} ${JSON.stringify(args).slice(0, 80)}\n   ${text.slice(0, show).replace(/\n\s*/g, " ")}`);
  return res.isError ? text : JSON.parse(text);
}

const tools = await client.listTools();
console.log(`${tools.tools.length} tools:`, tools.tools.map((t) => t.name).join(", "));
const prompts = await client.listPrompts();
console.log(`${prompts.prompts.length} prompts`);

await call("serato_status");
// The test copy may point at moved files: fix them first (force: the real Serato may be running, but this is a copy).
const dry = await call("relocate_missing_tracks", { search_dirs: [join(homedir(), "Music")] });
if (dry.matched?.length && backend === "legacy") await call("relocate_missing_tracks", { search_dirs: [join(homedir(), "Music")], dry_run: false, force: true });
const found = await call("search_tracks", { query: "", bpm_min: 120, bpm_max: 130, sort: "bpm", limit: 5 });
const a = found.tracks[0]?.path, b = found.tracks[1]?.path;
await call("search_tracks", { compatible_with_key: "8A", limit: 3 });
await call("get_track", { track: a }, { show: 600 });
await call("library_stats");
await call("find_duplicates");
await call("list_crates");
const crates = await call("list_crates");
if (crates[0]) await call("get_crate", { name: crates[0].name, limit: 2 });
await call("compatible_keys", { key: "Am" });
await call("analyze_transition", { from: a, to: b }, { show: 500 });
await call("suggest_next_tracks", { track: a, energy: "up", limit: 3 }, { show: 500 });
const plan = await call("plan_mix", { duration_min: 45, curve: "build", save_as_crate: "MCP E2E / Set", export_m3u: join(work, "set.m3u8"), force: true }, { show: 700 });
console.log("   keyPath:", plan.summary?.keyPath, "| length", plan.summary?.estimatedLength, "| avg", plan.summary?.averageTransitionScore);
await call("analyze_set", { crate: "MCP E2E / Set", reorder: true }, { show: 300 });
await call("plan_mix", { count: 6, start_with: a, end_with: b, harmonic: "strict" }, { show: 200 });
await call("add_to_crate", { name: "MCP E2E / Set", tracks: [a], force: true });
await call("remove_from_crate", { name: "MCP E2E / Set", tracks: [a], force: true });
const editTarget = backend === "legacy" ? a : (await call("get_crate", { name: "TechHouse" }, { show: 0 })).tracks[1].path;
await call("update_tracks", { edits: [{ track: editTarget, comment: "edited by e2e", color: "#00ff00" }], force: true });
if (backend === "legacy") {
  const edited = await call("get_track", { track: a }, { show: 0 });
  if (edited.comment !== "edited by e2e" || edited.color !== "#00ff00") { failures++; console.log("FAIL edit not visible", edited.comment, edited.color); }
} else {
  const pending = (await call("list_crates", {}, { show: 0 })).filter((c) => c.pendingImport).map((c) => c.name);
  if (!pending.includes("MCP E2E / Set")) { failures++; console.log("FAIL new crate not shown as pendingImport", pending); } else console.log("PASS new crate listed as pendingImport");
  // Editing a crate that exists in Serato 4 and has a legacy twin file.
  const th = await call("get_crate", { name: "TechHouse" }, { show: 0 });
  const r = await call("remove_from_crate", { name: "TechHouse", tracks: [th.tracks[0].path], force: true });
  if (r.removed !== 1) { failures++; console.log("FAIL remove from Serato 4 crate", r); }
  await call("create_crate", { name: "Spotify: Your Library", tracks: [a], mode: "append", force: true }, { expectError: true });
}
await call("rename_crate", { name: "MCP E2E", new_name: "MCP E2E Renamed", force: true });
await call("delete_crate", { name: "MCP E2E Renamed", force: true });
await call("export_playlist", { path: join(work, "c.m3u8"), tracks: [a, b] });
await call("list_sessions");
await call("now_playing");
await call("midi_mapping", {}, { show: 150 });
await call("create_crate", { name: "Bad:Name", tracks: [a], force: true }, { expectError: true });
await call("get_track", { track: "zzzz-no-such-track-qqq" }, { expectError: true });
await call("search_tracks", { key: "H#" }, { expectError: true });
// Without force, writes must refuse while Serato runs (only meaningful when it is running).
const st = await call("serato_status", {}, { show: 0 });
if (st.seratoRunning) await call("create_crate", { name: "ShouldNotExist", tracks: [a] }, { expectError: true });

console.log("m3u:", readFileSync(join(work, "set.m3u8"), "utf8").split("\n").slice(0, 3).join(" | "));
await client.close();
console.log(failures ? `\n${failures} FAILURES` : "\nALL PASSED", "work dir:", work);
process.exit(failures ? 1 : 0);
