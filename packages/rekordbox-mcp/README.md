# rekordbox-mcp

An [MCP](https://modelcontextprotocol.io) server for your local **rekordbox 6/7** library. It reads
rekordbox's own database directly through [pyrekordbox](https://github.com/dylanljones/pyrekordbox), so
there's nothing to export, and it sees changes as you make them in rekordbox.

## Install

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed:

**Claude Desktop** (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "rekordbox": {
      "command": "uvx",
      "args": ["rekordbox-mcp"]
    }
  }
}
```

**Claude Code:**

```sh
claude mcp add rekordbox -- uvx rekordbox-mcp
```

**Other clients** (Cursor, Windsurf, VS Code…): use `"command": "uvx", "args": ["rekordbox-mcp"]`.

To allow playlist editing, add `"env": { "REKORDBOX_MCP_ALLOW_WRITE": "1" }`
(Claude Code: `claude mcp add rekordbox -e REKORDBOX_MCP_ALLOW_WRITE=1 -- uvx rekordbox-mcp`).

## Tools

**Read** (safe while rekordbox is open)

| Tool | What it does |
| --- | --- |
| `library_stats` | Track and playlist counts, BPM range, top genres, keys and artists |
| `search_tracks` | Filter by text, artist, genre, key (`Am` or `8A`), BPM range, rating or My Tag; sortable |
| `get_track` | Full metadata, file path, My Tags, memory cues and hot cues |
| `list_playlists` / `get_playlist_tracks` | Playlist tree and contents, smart playlists included |
| `list_history` / `get_history_tracks` | Play-history sessions |
| `list_my_tags` | My Tag categories and tags |
| `find_compatible_tracks` | Next-track ideas: harmonically compatible key (Camelot wheel) and BPM within a tolerance; half and double time count |
| `build_set` | Order a playlist or list of tracks into a set with an energy curve (`build`, `peak`, `cooldown`, `smooth`), grading each transition smooth, workable or hard |
| `find_duplicate_tracks` | Tracks that share a title and artist |

**Write** (playlists only; tracks and audio files are never changed)

`create_playlist`, `create_playlist_folder`, `add_tracks_to_playlist`, `remove_tracks_from_playlist`,
`rename_playlist`, `delete_playlist`

Writes are guarded three ways:

1. They're **off** unless `REKORDBOX_MCP_ALLOW_WRITE=1` is set.
2. They're **refused while rekordbox is running**. rekordbox keeps its own copy of the library in memory and
   can overwrite outside changes. Quit rekordbox, make the changes, reopen it.
3. Before the first write of each session, `master.db` (with its `-wal`/`-shm` files) and
   `masterPlaylists6.xml` are copied to `rekordbox-mcp-backups/<timestamp>/` inside your rekordbox folder
   (`~/Library/Pioneer/rekordbox` on macOS, `%APPDATA%\Pioneer\rekordbox` on Windows).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `REKORDBOX_MCP_ALLOW_WRITE` | off | `1` enables the write tools |
| `REKORDBOX_DB_DIR` | auto-detected | Folder containing `master.db`, e.g. a copy to experiment on |

## Notes

- Keys are reported both as rekordbox stores them (`Am`, `8A`…) and in Camelot notation.
- BPM is in beats per minute, ratings are 0–5 stars, and durations and cue positions are in seconds. Hot
  cues are slots A–H.
- Search loads the collection into memory, which is fine for libraries of tens of thousands of tracks.

## Troubleshooting

- **"No Rekordbox v6/v7 directory found"**: rekordbox isn't installed in the usual place. Set
  `REKORDBOX_DB_DIR` to the folder containing `master.db`.
- **The database can't be unlocked after a rekordbox update**: update pyrekordbox
  (`uvx --refresh rekordbox-mcp`). If that doesn't help, check its issue tracker.
- **"rekordbox is running" when writing**: quit rekordbox completely. The background `rekordboxAgent`
  doesn't count.

## Development

From a checkout of the repo: `uv run pytest packages/rekordbox-mcp`. Read tests use your local library
read-only and skip if there isn't one. Write tests copy the library to a temp folder, check that the copy
is what got opened, and only then write.
