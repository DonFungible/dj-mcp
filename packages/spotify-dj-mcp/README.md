# spotify-dj-mcp

An MCP server for your Spotify playlists and Liked Songs that can turn them into a DJ set, using your
rekordbox library for BPM and key.

Spotify stopped giving BPM and key to new apps in November 2024, and its tracks can't be played in DJ
software. So `build_mix` matches your Spotify songs to files you own in rekordbox, orders those into a set,
and lists the rest as songs you don't own yet.

## Tools

| Tool | What it does |
| --- | --- |
| `spotify_status` | Whether you're logged in, and as whom |
| `list_playlists` | Your playlists with IDs and track counts |
| `get_playlist_tracks` | A playlist's tracks (takes an ID, `spotify:playlist:` URI or open.spotify.com link) |
| `get_liked_songs` | Your Liked Songs, newest first |
| `search_spotify` | Track search (max 10 results; Spotify's limit for personal apps) |
| `create_playlist` | Make a new playlist (private by default) from track URIs, in order |
| `build_mix` | Liked Songs and/or playlists → matched to rekordbox → ordered set with graded transitions |

`build_mix` options: `curve` is `build` (rising BPM), `peak` (up then down), `cooldown` or `smooth`;
`max_tracks` keeps the songs nearest the median tempo when more match. It returns:

- the set in order, with each transition graded smooth / workable / hard and why (key move, BPM change);
- `rekordbox_track_ids`, to save it with the rekordbox server's `create_playlist`;
- `spotify_uris`, to save it here with `create_playlist`;
- `different_version_only`: songs where you own another version (e.g. a remix instead of the original);
- `not_in_library`: songs you don't own.

Matching treats "Extended Mix", "Original Mix", "Radio Edit", "[Explicit]", "(feat. …)" and "Remastered" as
the same song, and remixes as different songs. It ignores accents and case.

## Setup

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and a Spotify account. Since February
2026, Spotify requires the owner of a personal ("Development Mode") app to have Premium.

1. **Create a Spotify app** at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard):
   - **Redirect URI:** `http://127.0.0.1:8888/callback`
   - **APIs used:** Web API
   - After creating it, open **User Management** and add the email of the Spotify account you'll use.
   - Copy the **Client ID**. Login uses PKCE, so no client secret is needed.
2. **Log in once** in a terminal:
   ```sh
   SPOTIFY_CLIENT_ID=your-client-id uvx spotify-dj-mcp login
   ```
   Your browser opens Spotify's consent page. The token is saved to `~/.config/spotify-dj-mcp/token.json`
   (readable only by you) and refreshed automatically. `uvx spotify-dj-mcp status` checks it, and
   `logout` deletes it.
3. **Add the server to your MCP client.** Claude Desktop (Settings → Developer → Edit Config):
   ```json
   {
     "mcpServers": {
       "spotify": {
         "command": "uvx",
         "args": ["spotify-dj-mcp"],
         "env": { "SPOTIFY_CLIENT_ID": "your-client-id" }
       }
     }
   }
   ```
   Claude Code: `claude mcp add spotify -e SPOTIFY_CLIENT_ID=your-client-id -- uvx spotify-dj-mcp`

`build_mix` also needs rekordbox installed on the same computer. The other tools work without it.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPOTIFY_CLIENT_ID` | (required) | Your Spotify app's Client ID. Can also go in a `.env` file in the working directory |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8888/callback` | Must exactly match the app's redirect URI |
| `REKORDBOX_DB_DIR` | auto-detected | Folder containing rekordbox's `master.db`, used by `build_mix` |

## Troubleshooting

- **403 errors:** check that the app owner has Premium and that your account is listed under User
  Management. Spotify's dashboard can take a few minutes to apply changes.
- **"INVALID_CLIENT: Invalid redirect URI"**: the redirect URI in the dashboard must be exactly
  `http://127.0.0.1:8888/callback`. Spotify no longer accepts `localhost`.
- **Port 8888 is in use:** set `SPOTIFY_REDIRECT_URI` to another loopback port, e.g.
  `http://127.0.0.1:8899/callback`, and add that URI to the app too.

## API notes

This uses Spotify's February 2026 endpoints: `/playlists/{id}/items` (entries under `item`), and
`POST /me/playlists` to create playlists. It also accepts the older `tracks`/`track` field names.
Development Mode apps are limited to 5 users.
