# serato-mcp

An [MCP](https://modelcontextprotocol.io) server for **Serato DJ Pro** (macOS). It reads your Serato library,
crates, cue points and play history, plans complete harmonic DJ sets, manages crates, and drives the decks over
a virtual MIDI device.

Works with Serato DJ 4 (live SQLite library) and Serato DJ 3 / earlier (`database V2` + crate files).

## Install

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed (macOS only):

**Claude Desktop** (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "serato": {
      "command": "uvx",
      "args": ["serato-mcp"]
    }
  }
}
```

**Claude Code:**

```sh
claude mcp add serato -- uvx serato-mcp
```

**Other clients** (Cursor, Windsurf, VS Code…): use `"command": "uvx", "args": ["serato-mcp"]`.

To keep it strictly read-only, add `"env": { "SERATO_MCP_READONLY": "1" }`
(Claude Code: `claude mcp add serato -e SERATO_MCP_READONLY=1 -- uvx serato-mcp`).

## What it can do

| Area | Tools |
| --- | --- |
| Library | `serato_status`, `search_tracks` (text, BPM, key, harmonic match, energy, genre, crate), `get_track` (hot cues, loops, beatgrid, Mixed In Key data, mix points), `library_stats`, `find_duplicates` |
| Crates | `list_crates` (crates, smart crates, iTunes and streaming playlists), `get_crate`, `create_crate`, `add_to_crate`, `remove_from_crate`, `rename_crate`, `delete_crate` |
| Metadata | `update_tracks` (title, artist, genre, comment, key, BPM, colour…), `relocate_missing_tracks` |
| History | `list_sessions`, `get_session`, `now_playing`, `play_stats` (most played, favourite transitions) |
| Mixing | `plan_mix` (full set), `suggest_next_tracks`, `analyze_transition`, `analyze_set` (score and reorder a setlist), `compatible_keys`, `export_playlist` (.m3u8) |
| Decks (MIDI) | `midi_setup`, `midi_mapping`, `midi_learn`, `deck_control`, `deck_sequence`, `set_tempo`, `fader_sweep`, `auto_transition`, `midi_monitor` |
| App | `launch_serato`, `quit_serato`, `load_track`, `serato_search_box`, `send_keystroke` |

Prompts: `plan_set`, `whats_next`.

### Mix planning

`plan_mix` builds a set with a beam search over your library, scoring each step on:

- **Harmony**: Camelot-wheel relations (same key, ±1, relative major/minor, diagonal, +2 and +7 energy boosts).
  Clashes are penalised.
- **Tempo**: the pitch change needed, with half/double-time matching and a maximum jump (default 8%).
- **Energy**: fit to a curve (`warmup`, `build`, `peak`, `journey`, `steady`, `cooldown`, `rollercoaster`, or
  `custom_curve: [4,6,8,9,7]`). Energy comes from Mixed In Key (comment or file tag); otherwise it is estimated
  from BPM and flagged.
- **Flow**: no repeated artist within 3 tracks, a small bonus for genre continuity, duplicate copies of the same
  song removed.

Each set comes with start times, a play-from and mix-out point per track (from hot cues, the beatgrid, or phrase
length), and a technique for every transition (long blend, bass swap, filter/short blend for clashing keys,
echo-out for tempo gaps, key-lock warnings). The plan can be saved as a crate (`save_as_crate`) or exported as
`.m3u8`, and can be pinned with `start_with`, `end_with` and `must_include`. `exclude_played` leaves out tracks
from your last session.

## Deck control (one-time MIDI mapping)

While the server runs it publishes a virtual MIDI device called **Serato MCP**, using macOS's built-in CoreMIDI
(set `SERATO_MCP_MIDI_PORT` to use an existing port such as `IAC Driver Bus 1`, or `SERATO_MCP_MIDI=off` to
disable it).

1. Start your MCP client, open Serato DJ Pro, go to **Setup → MIDI** and enable **Serato MCP**.
2. Click **MIDI** at the top of Serato to enter assign mode.
3. Click a control in Serato (e.g. deck 1 play), then ask the assistant to run `midi_learn` for that action and
   deck. Repeat for each control you want.
4. Save the mapping preset in Setup → MIDI.

Deck *N* uses MIDI channel *N*; mixer/global controls use channel 16. `midi_mapping` lists every action:
transport, hot cues 1–8, loops and auto-loops, beat jump, tempo, key shift/sync, EQ, filter, gain, volume,
crossfader, FX, library browsing, sampler and record.

`auto_transition` performs a full blend over N bars: sync and start the incoming deck, fade it in, swap the bass,
fade the outgoing deck out and pause it.

`load_track` types the track into Serato's search box and loads it with Shift+←/→ (decks 1–2) or the mapped MIDI
load action (decks 3–4). It needs **Accessibility** permission for the app running your MCP client (System
Settings → Privacy & Security → Accessibility).

## Safety

- **Reads never modify anything.** Serato 4's library is opened read-only (safe while Serato runs).
- **Writes refuse while Serato DJ is running**, because Serato rewrites its library files on exit. Use
  `quit_serato` first, or pass `force: true` if you accept the risk.
- **Every write is backed up** to `~/.serato-mcp/backups/<timestamp>/` and written atomically.
- Writes only touch Serato's library files, never your audio files.
- `SERATO_MCP_READONLY=1` disables all writes.

### Serato DJ 4 notes

Serato DJ 4 keeps its live library in `~/Library/Application Support/Serato/Library/master.sqlite` and syncs with
the legacy files in `~/Music/_Serato_`: it exports changes to them, and re-imports them when their contents
change. serato-mcp reads the SQLite library and writes crate changes to the legacy files, so **Serato 4 picks up
crate changes the next time it starts**. Until then, new crates show as `pendingImport` in `list_crates`. Check the
first few changes in Serato after you relaunch it.

- iTunes and streaming playlists are read-only.
- `update_tracks` only covers tracks that exist in the legacy `database V2`.
- Streaming tracks (Spotify etc.) are left out of searches and mix plans unless you pass `include_streaming: true`.
  They can't be added to crate files.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SERATO_ROOTS` | `~/Music/_Serato_` + `/Volumes/*/_Serato_` | Legacy `_Serato_` folders, `:`-separated (`dir` or `dir=volumeRoot`) |
| `SERATO_SQLITE_DIR` | `~/Library/Application Support/Serato/Library` | Serato 4 library folder |
| `SERATO_BACKEND` | auto | `legacy` to ignore the Serato 4 SQLite library |
| `SERATO_MCP_READONLY` | off | `1` disables writes |
| `SERATO_MCP_BACKUP_DIR` | `~/.serato-mcp/backups` | Backup location |
| `SERATO_MCP_MIDI_PORT` | `Serato MCP` | MIDI port name to create or use |
| `SERATO_MCP_MIDI` | on | `off` disables the MIDI device |
| `SERATO_APP_NAME` | `Serato DJ Pro` | App name for launch/quit/keyboard control |

## Development

From a checkout of the repo: `uv run pytest packages/serato-mcp`. Tests that read your Serato library only read
it, and write tests work on a temporary copy.

- `binary.py`: Serato's tag-length-value container (database V2, crates), round-trip byte-exact.
- `tags.py`: `Serato Markers2` (cues, loops, colour), `Serato BeatGrid`, `Serato Autotags`, Mixed In Key frames
  (ID3 GEOB, MP4 and FLAC).
- `sqlite.py`: Serato DJ 4 library (assets, containers, history). `library.py`: the combined read model.
- `writer.py`: crate and metadata edits with backups.
- `keys.py`, `planner.py`: Camelot keys, transition scoring and set planning.
- `coremidi.py`, `midi.py`: the virtual MIDI device. `app.py`: macOS app control.
