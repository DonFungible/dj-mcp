# Security

Please report vulnerabilities privately through
[GitHub security advisories](https://github.com/DonFungible/dj-mcp/security/advisories/new) rather than in a
public issue. You should hear back within a week.

## What these tools can access

- **rekordbox-mcp** reads your rekordbox database. With `REKORDBOX_MCP_ALLOW_WRITE=1` it can change
  playlists. It runs locally and makes no network requests.
- **spotify-dj-mcp** stores a Spotify OAuth token in `~/.config/spotify-dj-mcp/token.json` with
  owner-only permissions. It requests read access to your library and playlists, and permission to create
  playlists. It talks only to Spotify's servers.
- **midi-mapper-flx4** can open apps, press keys and run shell commands that you (or your AI assistant, through
  its MCP tools) configure. Its editor is a local web server
  bound to `127.0.0.1` that requires a random per-session token on every API call and rejects other
  `Host`/`Origin` headers, so other websites can't add mappings. Treat the config file like a shell script.

MCP servers act on behalf of whatever AI client you connect them to. Only enable write tools for clients
and prompts you trust.
