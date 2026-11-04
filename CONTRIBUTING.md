# Contributing

Thanks for helping. Bug reports from setups other than macOS + rekordbox 7 + DDJ-FLX4 are especially
useful, since that's the only combination tested so far.

## Setup

```sh
git clone https://github.com/DonFungible/dj-mcp && cd dj-mcp
uv sync                 # one .venv with every package installed in editable mode
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Some tests need things that may not be on your machine. They skip themselves when these are missing:

- **A local rekordbox library:** read tests use it read-only. Write tests copy it into a temp folder and
  check that the copy is what was opened before writing anything.
- **A local Serato library:** format tests read it, and crate tests write to a copy in a temp folder.
- **Virtual MIDI ports:** available on macOS, and on Linux with the ALSA sequencer, but not on Windows.
  serato-mcp's MIDI tests use CoreMIDI and only run on macOS.

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
  serato-mcp/       binary.py, tags.py, sqlite.py, library.py (Serato's formats), writer.py (crate/metadata edits),
                    keys.py, planner.py (set planning), coremidi.py, midi.py, app.py (deck and app control), server.py
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

1. Create `packages/<name>/` with `README.md`, `LICENSE` (copy the root one) and a `pyproject.toml`. Copy the
   metadata layout from an existing package.
2. Add it to the root `pyproject.toml` (`dependencies` and `[tool.uv.sources]`), then `uv sync`. Avoid
   dependencies without ready-made wheels for current Python versions, so `uvx <name>` works anywhere.
3. Add it to the table in the root README and to `.mcp.json` if it's an MCP server.
4. Check the name is free: `curl -s -o /dev/null -w "%{http_code}" https://pypi.org/pypi/<name>/json`
   should print `404`.

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
   trusted publishing to PyPI.

Release `rekordbox-mcp` before a `spotify-dj-mcp` version that needs new rekordbox features.
