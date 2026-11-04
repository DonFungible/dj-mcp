# Contributing

Thanks for helping. Bug reports from setups other than macOS + rekordbox 7 + DDJ-FLX4 are especially
useful, since that's the only combination tested so far.

## Setup

```sh
git clone https://github.com/DonFungible/dj-mcp && cd dj-mcp
uv sync                 # one .venv with every package installed in editable mode
uv run pytest
uv run ruff check . && uv run ruff format --check .

# serato-mcp (TypeScript, Node 22.13+, macOS)
cd packages/serato-mcp && npm ci && npm run build
npm test            # unit tests + MIDI loopback
npm run test:e2e    # full MCP round-trip on a temporary copy of your Serato library
npm run test:live   # read-only smoke test against your real library
```

Some tests need things that may not be on your machine. They skip themselves when these are missing:

- **A local rekordbox library:** read tests use it read-only. Write tests copy it into a temp folder and
  check that the copy is what was opened before writing anything.
- **Virtual MIDI ports:** available on macOS, and on Linux with the ALSA sequencer, but not on Windows.

Never point a test that writes at a real library. Use `RekordboxLibrary(db_dir=<copy>)` and assert on
`lib.db.engine.url.database` first, as `tests/test_writes_on_copy.py` does.

To try your changes in Claude Code, open the repo: `.mcp.json` runs the servers from your checkout.

## Layout

```
packages/
  rekordbox-mcp/    library.py does the work and has no MCP dependency; server.py wraps it as MCP tools
  spotify-dj-mcp/   client.py (Web API), auth.py (PKCE login), matching.py, server.py
  midi-mapper-flx4/ config.py, engine.py (actions), runtime.py (device loop), flx4.py (control names),
                    service.py (background mapper, button capture), server.py (MCP), gui.py + web/index.html
  serato-mcp/       TypeScript: serato/ (file formats, library, writer), mix/ (keys, planner), control/ (MIDI, app)
```

Keep logic in plain modules that can be tested without MCP, and keep `server.py` thin.

## Rules for tools that change data

Anything that writes to a user's DJ library or account must:

1. be off by default, behind an environment variable;
2. refuse to run while the DJ app is open, if the app could overwrite the change;
3. back up what it's about to change, once per session, before the first write;
4. be annotated with `read_only_hint=False` (and `destructive_hint=True` if it deletes);
5. report failures as `ToolError` with a message that says what to do next.

## Adding a server

1. Create `packages/<name>/` with `README.md`, `LICENSE` (copy the root one) and either a `pyproject.toml`
   (Python) or a `package.json` (Node). Copy the metadata layout from an existing package.
2. Python: add it to the root `pyproject.toml` (`dependencies` and `[tool.uv.sources]`), then `uv sync`.
   Node: add it to `exclude` under `[tool.uv.workspace]` in the root `pyproject.toml`, and to the CI workflow.
3. Add it to the table in the root README and to `.mcp.json` if it's an MCP server.
4. Check the name is free: `curl -s -o /dev/null -w "%{http_code}" https://pypi.org/pypi/<name>/json`
   should print `404` (for npm: `npm view <name>` should fail with E404).

Ideas for new servers: Traktor, Engine DJ, VirtualDJ, Beatport and SoundCloud.

## Pull requests

- Keep changes focused, and include tests for behaviour changes.
- Note user-visible changes in `CHANGELOG.md` under "Unreleased".
- CI runs lint and tests on macOS, Linux and Windows.

## Releases (maintainers)

Each package is versioned and released on its own:

1. Bump `version` in `packages/<name>/pyproject.toml` and move the CHANGELOG entries under the new version.
2. Tag and push: `git tag <name>-v<version> && git push origin <name>-v<version>`
   (e.g. `rekordbox-mcp-v0.1.1`).
3. The `release` workflow builds that package, checks the tag matches its version, and publishes it with
   trusted publishing: Python packages to PyPI, serato-mcp to npm.

Release `rekordbox-mcp` before a `spotify-dj-mcp` version that needs new rekordbox features.
