# Changelog

Each package is released separately; entries are grouped by package.

## Unreleased

## rekordbox-mcp 0.1.0

- Read tools: library stats, track search (text, key, BPM, rating, My Tag), track details with cues,
  playlists (smart playlists included), history and My Tags.
- `find_compatible_tracks`: harmonic (Camelot) and tempo matching, including half and double time.
- `build_set`: orders tracks into a set along an energy curve and grades every transition.
- Playlist editing, off by default, refused while rekordbox is running, with a backup before the first write.

## spotify-dj-mcp 0.1.0

- Playlists, Liked Songs, search and playlist creation on Spotify's February 2026 Web API.
- `build_mix`: matches Spotify songs to your rekordbox library and orders the matches into a set.
- PKCE login (client ID only) with automatic token refresh.

## serato-mcp 0.1.0

- Serato DJ Pro 3 (`database V2` + crates) and Serato DJ 4 (SQLite library) support.
- Library search with cues, loops, beatgrid and Mixed In Key energy; crates and smart crates; play history.
- `plan_mix` (beam-search set planner with energy curves, mix points and transition techniques),
  `suggest_next_tracks`, `analyze_set`, `.m3u8` export.
- Crate and metadata editing with backups, refused while Serato is running.
- Deck control over a virtual MIDI device, and loading tracks via Serato's search box.
- Python, run with `uvx serato-mcp` like the other servers. The MIDI device uses macOS's CoreMIDI directly, so
  there's nothing to compile.

## midi-mapper-flx4 0.1.0

- MCP server: press a button on the DDJ-FLX4 and tell your AI assistant what it should do
  (`wait_for_button`, `map_button`, `test_mapping`, `unmap_button`, `status`, `start_mapper`, `stop_mapper`).
- Map FLX4 buttons, pads and jog wheels to apps, key combos, text, shell commands, scrolling and per-step keys.
  Controls are shown by name ("Deck 1 Play/Pause", "Deck 2 Hot Cue pad 3").
- A background mapper that keeps running after the assistant closes, with one mapper at a time.
- `midi-mapper-flx4 map`: an interactive drawing of the FLX4 in the terminal that lights up the control you
  press and lets you map it on the spot. The MCP tools return the same drawing.
- Browser-based editor with live activity, learn-by-pressing and key-combo recording.
- Pauses while rekordbox or Serato is running, reconnects when the controller is replugged, and reloads the
  config live.
