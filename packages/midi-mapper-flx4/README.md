# midi-mapper-flx4

Turn a Pioneer **DDJ-FLX4** into a macro pad that you set up by talking to your AI assistant. Press a
button, say what it should do ("open Discord", "press ⌘⇧M", "type /clear and Enter"), and it's mapped.

It's an [MCP](https://modelcontextprotocol.io) server, plus a command line and a browser-based editor if you'd
rather map buttons yourself. It pauses itself while rekordbox or Serato is running, so the FLX4 goes back
to being a DJ controller.

## Set up

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed:

**Claude Code:**

```sh
claude mcp add flx4 -- uvx --python 3.12 midi-mapper-flx4
```

**Claude Desktop** (Settings → Developer → Edit Config), Cursor and other MCP clients:

```json
{
  "mcpServers": {
    "flx4": {
      "command": "uvx",
      "args": ["--python", "3.12", "midi-mapper-flx4"]
    }
  }
}
```

(`--python 3.12` picks a Python that the MIDI library, python-rtmidi, ships ready-made builds for. On 3.13+
it has to be compiled, which needs a C++ compiler.)

## Map a button

Plug in the FLX4 and ask:

> I want to map a button on my FLX4 to open Discord.

The assistant tells you to press the button, waits for it, and saves the mapping:

> Mapped **Deck 1 Hot Cue pad 1** to open Discord.

Some other things to ask:

- *"Make Shift + Play on deck 2 run my tests: `cd ~/code/app && uv run pytest`."*
- *"Turn the left jog wheel into a scroll wheel."*
- *"What are my FLX4 buttons mapped to?"* / *"Remove the Discord button."* / *"Try the Discord button."*

Mappings are carried out by the **mapper**, a small background process the assistant starts when you map
your first button. It keeps running after you close the assistant. Stop it with
`uvx --python 3.12 midi-mapper-flx4 stop` (or ask the assistant to).

**macOS:** opening apps and running commands work straight away. Key combos, typing and scrolling need
Accessibility permission for the app that started the mapper (usually your terminal): System Settings →
Privacy & Security → Accessibility. Then restart the mapper. The assistant tells you when it's missing.

## Actions

| Action | Example | Does |
| --- | --- | --- |
| App | `Discord` | Opens the app, or brings it to the front if it's already open |
| Keys | `cmd+shift+p` | Presses a combo. Modifiers: `cmd`, `ctrl`, `alt`/`option`, `shift`. Keys: letters, digits, punctuation, `enter`, `esc`, `tab`, `space`, `backspace`, `delete`, arrows (`up`…), `home`, `end`, `pageup`, `pagedown`, `f1`–`f20`, `plus` |
| Text | `/clear` + Enter | Types text; each line break presses Enter |
| Command | `say done` | Runs a shell command in the background; output goes to `~/.config/midi-mapper-flx4/shell.log` |
| Scroll | 3 lines per step | Jog wheels: scrolls the window under the mouse |
| Turn | `down` / `up` | Jog wheels: a combo per clockwise or counter-clockwise step |

Shift + a button counts as a separate button, so each one can do two things. Buttons fire on press, not
release.

## MCP tools

| Tool | What it does |
| --- | --- |
| `wait_for_button` | Waits for you to press a button, pad or jog wheel, and returns its name and current mapping. Presses are ignored by the mapper meanwhile |
| `map_button` | Saves an action for a control (replacing any existing one) and starts the mapper if needed |
| `test_mapping` | Runs a button's action once, as if pressed |
| `unmap_button` | Removes a mapping |
| `status` | FLX4 connected? Mapper running? Accessibility allowed? Plus every mapping |
| `start_mapper` / `stop_mapper` | Starts or stops the background mapper |

## Map buttons in the terminal

```sh
uvx --python 3.12 midi-mapper-flx4 map
```

This draws the FLX4 in your terminal. Press any button, pad, knob or jog wheel and it lights up, with its name
and what it does now. Then press a letter to choose an action (`a` open an app, `k` key combo, `t` type text,
`c` shell command; `s` scroll for jog wheels), `x` to remove a mapping, `r` to try it, or just press another
control. Mapped controls are shown in green and in parentheses:

```
╭───────────────────────────────────── Pioneer DDJ-FLX4 ─────────────────────────────────────╮
│                                       [ BEAT FX ON ]                                       │
│  [LOOP IN] [LOOP OUT]  [ SYNC ]   [LOAD] [BROWSE] [LOAD]   [LOOP IN] [LOOP OUT]  [ SYNC ]  │
│  DECK 1                                                    DECK 2                          │
│    ║     .-~~~~~~~~~~~-.          [ TRIM][MASTER][ TRIM]           .-~~~~~~~~~~~-.    ║    │
│    ║    /               \         [  HI ][PHONES][  HI ]          /               \   ║    │
│    ║   |    (  JOG  )    |        [ MID ][HP MIX][ MID ]         |    [  JOG  ]    |  ║    │
│    ║    \               /         [ LOW ]        [ LOW ]          \               /   ║    │
│    ║     '-~~~~~~~~~~~-'          [ CFX ]        [ CFX ]           '-~~~~~~~~~~~-'    ║    │
│ [TEMPO]                           [ CUE ]        [ CUE ]                           [TEMPO] │
│  [HOTCUE][PADFX][JUMP][SAMPLE]       ║              ║      [HOTCUE][PADFX][JUMP][SAMPLE]   │
│  (  1  )[  2  ][  3  ][  4  ]     [FADER]        [FADER]   [  1  ][  2  ][  3  ][  4  ]    │
│  [  5  ][  6  ][  7  ][  8  ]               ╪              [  5  ][  6  ][  7  ][  8  ]    │
│  [ CUE ] ( PLAY )                    [  CROSSFADER  ]      [ CUE ] » PLAY «                │
╰────────────────────────────────────────────────────────────────────────────────────────────╯
```

Here deck 1's Play, deck 1's jog wheel and pad 1 are mapped, and deck 2's Play was just pressed. While `map` is
open, the background mapper ignores presses, so you can select a control without triggering it. When you quit,
it offers to start the mapper if it isn't running. Your assistant shows the same drawing in chat when you map a
button through MCP.

## Other commands

```sh
uvx --python 3.12 midi-mapper-flx4 gui      # browser editor; also runs the mappings while it's open
uvx --python 3.12 midi-mapper-flx4 run      # run the mappings in this terminal (add --dry-run to log only)
uvx --python 3.12 midi-mapper-flx4 status   # is the mapper running and the FLX4 connected?
uvx --python 3.12 midi-mapper-flx4 stop     # stop the background mapper
uvx --python 3.12 midi-mapper-flx4 check    # validate the config and list mappings
```

Only one mapper runs at a time. Stop the background one before running `gui` or `run`. Pass
`--config PATH` before the command to use a different config file.

## Config file

Mappings live in `~/.config/midi-mapper-flx4/config.toml`. The assistant, `map` and the editor write it for
you, and a running mapper reloads it when it changes. Saving from the assistant or the editor rewrites the
file, so hand-written comments are dropped.

```toml
pause_when_running = ["rekordbox", "Serato DJ Pro", "Serato DJ Lite"]

[[map]]
name = "Open Discord"
note = 0
channel = 8           # deck 1 pads (Hot Cue mode)
app = "Discord"

[[map]]
name = "Run tests"
note = 71
channel = 2           # deck 2, Shift + Play/Pause
shell = "cd ~/code/app && uv run pytest; say tests finished"

[[map]]
name = "Scroll"
cc = 33
channel = 1           # deck 1 jog wheel
scroll = 3
```

Each `[[map]]` has one control (`note = N` or `cc = N`, plus `channel = 1`–`16`) and one action: `app`,
`keys`, `text`, `shell`, `scroll`, or `keys_cw` + `keys_ccw`.

### FLX4 controls

Decks 1 and 2 send on channels 1 and 2. Their performance pads send on channels 8 and 10 (9 and 11 with
Shift held), and the note depends on the pad mode: Hot Cue 0–7, Pad FX 1 16–23, Beat Jump 32–39,
Sampler 48–55. So the same pad can do something different in each mode. The jog wheels are CC 33 (side)
and 34 (top). You never need these numbers when mapping through the assistant or the editor.

## Platform notes

- **macOS:** fully tested.
- **Windows and Linux:** should work but are untested. On Windows, only one program can open the FLX4 at a
  time, so the mapper stops for a moment while `wait_for_button` listens. On Linux,
  installing needs a C compiler (for the `evdev` keyboard library), and keystrokes need an X11 session;
  Wayland compositors generally block them.
- The editor only listens on `127.0.0.1` and needs the per-session link it opens, because it can create
  shell-command mappings.

## Not yet

- Lighting pads as status indicators (e.g. red when an AI agent is waiting for you).
- Starting the mapper at login.
