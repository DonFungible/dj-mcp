# dj-mcp

Tools that let AI assistants work with your DJ library: [MCP](https://modelcontextprotocol.io) servers for
rekordbox, Serato and Spotify, plus one that turns a Pioneer DDJ-FLX4 into a macro pad you set up by talking.

| Package | What it does | Install |
| --- | --- | --- |
| [**rekordbox-mcp**](packages/rekordbox-mcp) | Search your rekordbox 6/7 library, browse playlists, cues and history, find tracks that mix well, order tracks into a set, and (optionally) edit playlists | `uvx rekordbox-mcp` |
| [**serato-mcp**](packages/serato-mcp) | Search your Serato library with cues and Mixed In Key energy, manage crates, plan harmonic sets, read play history, and drive the decks over MIDI (macOS) | `npx -y serato-mcp` |
| [**spotify-dj-mcp**](packages/spotify-dj-mcp) | Browse your Spotify playlists and Liked Songs, and turn them into a DJ set from the tracks you own in rekordbox | `uvx spotify-dj-mcp` |
| [**midi-mapper-flx4**](packages/midi-mapper-flx4) | Press a button on your DDJ-FLX4 and tell the AI what it should do ("open Discord"). Maps buttons, pads and jog wheels to apps, key combos, text and commands; also has a browser-based editor | `uvx --python 3.12 midi-mapper-flx4` |

Ask things like:

- *"Find tracks in my library between 124 and 128 BPM that mix well after Waterpark."*
- *"Order my Tech House playlist into a set that builds energy, and tell me which transitions are hard."*
- *"Which of my Spotify Liked Songs do I already own? Build a peak-time set from them and list the rest."*
- *"I'm going to press a button on my FLX4. Make it open Discord."*

## Quick start

The Python packages need [uv](https://docs.astral.sh/uv/getting-started/installation/), which provides `uvx`
and fetches the right Python automatically. serato-mcp needs [Node.js](https://nodejs.org) 22.13+ (`npx`).

### Claude Desktop

Open **Settings → Developer → Edit Config** and add the servers you want:

```json
{
  "mcpServers": {
    "rekordbox": {
      "command": "uvx",
      "args": ["rekordbox-mcp"]
    },
    "serato": {
      "command": "npx",
      "args": ["-y", "serato-mcp"]
    },
    "spotify": {
      "command": "uvx",
      "args": ["spotify-dj-mcp"],
      "env": { "SPOTIFY_CLIENT_ID": "your-client-id" }
    },
    "flx4": {
      "command": "uvx",
      "args": ["--python", "3.12", "midi-mapper-flx4"]
    }
  }
}
```

Restart Claude Desktop. The Spotify server needs a one-time login first; see its
[setup steps](packages/spotify-dj-mcp#setup).

### Claude Code

```sh
claude mcp add rekordbox -- uvx rekordbox-mcp
claude mcp add serato -- npx -y serato-mcp
claude mcp add spotify -e SPOTIFY_CLIENT_ID=your-client-id -- uvx spotify-dj-mcp
claude mcp add flx4 -- uvx --python 3.12 midi-mapper-flx4
```

### Cursor, Windsurf, VS Code and other MCP clients

Use the same `command`/`args`/`env` as the Claude Desktop example in your client's MCP settings
(for example `.cursor/mcp.json`).

### Not published yet?

Until the Python packages are on PyPI, install them straight from GitHub:

```sh
uvx --from "git+https://github.com/DonFungible/dj-mcp#subdirectory=packages/rekordbox-mcp" rekordbox-mcp
```

Use the same pattern for the other packages. `spotify-dj-mcp` depends on `rekordbox-mcp`, so add
`--with "git+https://github.com/DonFungible/dj-mcp#subdirectory=packages/rekordbox-mcp"`.
For serato-mcp before it's on npm, see [its install notes](packages/serato-mcp#install).

## Safety

These tools work on your real DJ library, so they're cautious by default:

- **rekordbox-mcp is read-only** unless you set `REKORDBOX_MCP_ALLOW_WRITE=1`. Even then, it only changes
  playlists (never tracks or files), refuses to write while rekordbox is open, and backs up the database
  before the first change of each session.
- **serato-mcp** can edit crates and track metadata (in Serato's library files, never your audio files).
  It refuses while Serato is running (unless a tool is called with `force: true`), backs up every file
  before changing it, and `SERATO_MCP_READONLY=1` turns writes off entirely.
- **spotify-dj-mcp** only creates new playlists (private by default); it never deletes or edits existing ones.
- **midi-mapper-flx4** pauses itself while rekordbox or Serato is running, so your FLX4 only drives the DJ
  app. Its editor is served on `127.0.0.1` only and requires a per-session link.

rekordbox's database format is not officially documented. Writing to it relies on the community
[pyrekordbox](https://github.com/dylanljones/pyrekordbox) project. Keep your own backups.

## Compatibility

| | Tested on | Should work on |
| --- | --- | --- |
| rekordbox-mcp | macOS, rekordbox 7.2 | Windows, rekordbox 6.x and 7.x |
| serato-mcp | macOS, Serato DJ Pro 3 and 4 | macOS only (uses AppleScript and macOS paths) |
| spotify-dj-mcp | macOS | Windows, Linux (rekordbox library optional for browsing) |
| midi-mapper-flx4 | macOS, Pioneer DDJ-FLX4 | Windows, Linux (DDJ-FLX4 only) |

Reports from other setups are very welcome. Please [open an issue](https://github.com/DonFungible/dj-mcp/issues).

## Development

The Python packages form a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/), one per
directory under `packages/`. serato-mcp is a TypeScript package in the same folder, managed with npm.

```sh
git clone https://github.com/DonFungible/dj-mcp && cd dj-mcp
uv sync              # creates .venv with every package installed in editable mode
uv run pytest        # tests that need rekordbox or virtual MIDI ports skip themselves when unavailable
uv run ruff check .

cd packages/serato-mcp && npm ci && npm run build && npm test   # the Node package
```

Opening the repo in Claude Code picks up the servers from [`.mcp.json`](.mcp.json), running from your
checkout (read-only by default; build serato-mcp first). See [CONTRIBUTING.md](CONTRIBUTING.md) for
conventions and how to add a server.

## License

[MIT](LICENSE).

Not affiliated with or endorsed by AlphaTheta (Pioneer DJ), Serato or Spotify. rekordbox, Serato and
Spotify are trademarks of their respective owners.
