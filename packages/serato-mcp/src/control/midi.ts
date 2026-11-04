/**
 * Deck/mixer control over MIDI.
 *
 * serato-mcp opens a virtual MIDI device ("Serato MCP") that Serato DJ Pro sees
 * like any controller. Every action has a fixed MIDI message (see ACTIONS); the
 * user maps each one once in Serato's MIDI assign mode, using `midi_learn` to
 * send the message on demand. After that the tools drive Serato directly.
 *
 * Layout: deck N uses MIDI channel N (1-4). Mixer/global/library controls use
 * channel 16. Buttons are notes (note-on 127, then note-off); faders/knobs are CCs.
 */
import midi from "@julusian/midi";

export const PORT_NAME = process.env.SERATO_MCP_MIDI_PORT ?? "Serato MCP";
const GLOBAL_CH = 16;

export type ActionKind = "button" | "fader";

export interface ActionDef {
  kind: ActionKind;
  scope: "deck" | "global";
  number: number; // note or CC
  description: string;
  serato: string; // where to click in Serato's MIDI assign mode
}

const btn = (number: number, description: string, serato: string, scope: "deck" | "global" = "deck"): ActionDef => ({ kind: "button", scope, number, description, serato });
const fader = (number: number, description: string, serato: string, scope: "deck" | "global" = "deck"): ActionDef => ({ kind: "fader", scope, number, description, serato });

export const ACTIONS: Record<string, ActionDef> = {
  // transport
  play_pause: btn(0, "Play / pause", "Deck play button"),
  cue: btn(1, "Cue (temporary cue)", "Deck cue button"),
  sync: btn(2, "Sync on/off", "Deck SYNC button"),
  load_selected: btn(3, "Load the highlighted library track onto this deck", "Load-to-deck: assign via Library > 'Load to Deck N' in MIDI assign"),
  key_lock: btn(4, "Key lock on/off", "Deck key lock (padlock) button"),
  censor: btn(5, "Censor / reverse while held", "Deck censor button"),
  slip: btn(6, "Slip mode on/off", "Deck slip button"),
  eject: btn(7, "Eject track", "Deck eject button"),
  // hot cues 1-8
  ...Object.fromEntries(
    Array.from({ length: 8 }, (_, i) => [`hotcue_${i + 1}`, btn(10 + i, `Trigger hot cue ${i + 1} (sets it if empty)`, `Hot cue ${i + 1} pad on the deck`)])
  ),
  ...Object.fromEntries(
    Array.from({ length: 8 }, (_, i) => [`hotcue_${i + 1}_delete`, btn(18 + i, `Delete hot cue ${i + 1}`, `Hot cue ${i + 1} delete (x) button`)])
  ),
  // loops
  loop_in: btn(30, "Manual loop in", "Loop IN"),
  loop_out: btn(31, "Manual loop out", "Loop OUT"),
  reloop: btn(32, "Reloop / exit loop", "Reloop / loop on-off"),
  loop_halve: btn(33, "Halve loop length", "Auto loop ÷2 (decrease)"),
  loop_double: btn(34, "Double loop length", "Auto loop ×2 (increase)"),
  autoloop_toggle: btn(35, "Auto loop on/off at the selected size", "Auto loop on/off button"),
  ...Object.fromEntries(
    ([1, 2, 4, 8, 16, 32] as const).map((beats, i) => [`autoloop_${beats}`, btn(36 + i, `Set a ${beats}-beat auto loop`, `Auto loop size ${beats}`)])
  ),
  beatjump_forward: btn(42, "Beat jump forward", "Beat jump >"),
  beatjump_back: btn(43, "Beat jump back", "Beat jump <"),
  // tempo / key
  pitch_reset: btn(50, "Reset tempo to 0%", "Tempo reset (double-click the pitch %)"),
  nudge_up: btn(51, "Temporary pitch bend up (held for ~250ms)", "Pitch bend +"),
  nudge_down: btn(52, "Temporary pitch bend down (held for ~250ms)", "Pitch bend -"),
  key_up: btn(53, "Key shift up a semitone", "Key shift +"),
  key_down: btn(54, "Key shift down a semitone", "Key shift -"),
  key_reset: btn(55, "Reset key shift", "Key reset"),
  key_sync: btn(56, "Key sync to the other deck", "Key sync"),
  // effects (per deck assign)
  fx1_assign: btn(60, "Assign FX unit 1 to this deck", "FX 1 deck assign button"),
  fx2_assign: btn(61, "Assign FX unit 2 to this deck", "FX 2 deck assign button"),
  // faders (CC, value 0-127)
  tempo: fader(1, "Tempo fader (64 = 0%)", "Deck tempo slider"),
  volume: fader(7, "Channel volume", "Channel fader"),
  gain: fader(8, "Channel gain/trim", "Channel gain knob"),
  eq_high: fader(16, "EQ high (64 = flat)", "EQ high knob"),
  eq_mid: fader(17, "EQ mid (64 = flat)", "EQ mid knob"),
  eq_low: fader(18, "EQ low (64 = flat)", "EQ low knob"),
  filter: fader(19, "Filter (64 = off, lower = low-pass, higher = high-pass)", "Channel filter knob"),
  // global / mixer (channel 16)
  crossfader: fader(8, "Crossfader (0 = left, 64 = centre, 127 = right)", "Crossfader", "global"),
  master_volume: fader(7, "Master volume", "Master volume", "global"),
  fx1_depth: fader(20, "FX unit 1 depth / wet-dry", "FX 1 depth knob", "global"),
  fx2_depth: fader(21, "FX unit 2 depth / wet-dry", "FX 2 depth knob", "global"),
  fx1_on: btn(20, "FX unit 1 effect 1 on/off", "FX 1 slot 1 on button", "global"),
  fx2_on: btn(21, "FX unit 2 effect 1 on/off", "FX 2 slot 1 on button", "global"),
  library_up: btn(0, "Move the library selection up", "Library browse up", "global"),
  library_down: btn(1, "Move the library selection down", "Library browse down", "global"),
  crate_up: btn(2, "Move the crate selection up", "Crate browse up", "global"),
  crate_down: btn(3, "Move the crate selection down", "Crate browse down", "global"),
  library_toggle_focus: btn(4, "Switch focus between crates and tracks", "Library focus toggle", "global"),
  record: btn(10, "Start/stop recording", "Record button", "global"),
  sampler_1: btn(30, "Fire sampler slot 1", "Sample player slot 1", "global"),
  sampler_2: btn(31, "Fire sampler slot 2", "Sample player slot 2", "global"),
  sampler_3: btn(32, "Fire sampler slot 3", "Sample player slot 3", "global"),
  sampler_4: btn(33, "Fire sampler slot 4", "Sample player slot 4", "global"),
};

export interface MidiMessageOut {
  action: string;
  channel: number;
  bytes: number[];
}

class MidiBridge {
  private out?: midi.Output;
  private input?: midi.Input;
  private virtual = false;
  private log: { at: string; bytes: number[] }[] = [];
  readonly feedback = new Map<string, number>();

  open(): { port: string; virtual: boolean } {
    if (this.out) return { port: PORT_NAME, virtual: this.virtual };
    const out = new midi.Output();
    // Use an existing port of that name (e.g. "IAC Driver Bus 1") if there is one.
    const idx = [...Array(out.getPortCount()).keys()].find((i) => out.getPortName(i) === PORT_NAME || out.getPortName(i).endsWith(PORT_NAME));
    if (idx !== undefined) out.openPort(idx);
    else {
      out.openVirtualPort(PORT_NAME);
      this.virtual = true;
    }
    this.out = out;
    // Listen for Serato's MIDI output (LED feedback) on a matching virtual input.
    try {
      const input = new midi.Input();
      const inIdx = [...Array(input.getPortCount()).keys()].find((i) => input.getPortName(i) === PORT_NAME);
      if (inIdx !== undefined) input.openPort(inIdx);
      else input.openVirtualPort(PORT_NAME);
      input.on("message", (_dt: number, msg: number[]) => {
        this.log.push({ at: new Date().toISOString(), bytes: msg });
        if (this.log.length > 200) this.log.shift();
        this.feedback.set(`${msg[0]}:${msg[1]}`, msg[2]);
      });
      this.input = input;
    } catch {
      /* feedback is optional */
    }
    return { port: PORT_NAME, virtual: this.virtual };
  }

  ports() {
    const o = new midi.Output();
    const i = new midi.Input();
    return {
      outputs: [...Array(o.getPortCount()).keys()].map((n) => o.getPortName(n)),
      inputs: [...Array(i.getPortCount()).keys()].map((n) => i.getPortName(n)),
    };
  }

  send(bytes: number[]) {
    this.open();
    this.out!.sendMessage(bytes as [number, number, number]);
  }

  received(limit = 50) {
    return this.log.slice(-limit);
  }

  close() {
    this.out?.closePort();
    this.input?.closePort();
    this.out = undefined;
    this.input = undefined;
  }
}

export const bridge = new MidiBridge();

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function channelFor(def: ActionDef, deck?: number): number {
  if (def.scope === "global") return GLOBAL_CH;
  if (!deck || deck < 1 || deck > 4) throw new Error("deck must be 1-4 for this action");
  return deck;
}

export function messageFor(action: string, deck?: number, value?: number): MidiMessageOut {
  const def = ACTIONS[action];
  if (!def) throw new Error(`Unknown action "${action}". Known: ${Object.keys(ACTIONS).join(", ")}`);
  const ch = channelFor(def, deck) - 1;
  if (def.kind === "button") return { action, channel: ch + 1, bytes: [0x90 | ch, def.number, 127] };
  const v = Math.max(0, Math.min(127, Math.round(value ?? 64)));
  return { action, channel: ch + 1, bytes: [0xb0 | ch, def.number, v] };
}

/** Press a button (note on, hold, note off) or move a fader. */
export async function perform(action: string, deck?: number, value?: number, holdMs?: number): Promise<MidiMessageOut> {
  const msg = messageFor(action, deck, value);
  bridge.send(msg.bytes);
  if (ACTIONS[action].kind === "button") {
    await sleep(holdMs ?? (action.startsWith("nudge") ? 250 : 40));
    bridge.send([0x80 | (msg.channel - 1), msg.bytes[1], 0]);
  }
  return msg;
}

/** Smoothly move a fader from one value to another over `durationMs`. */
export async function sweep(action: string, deck: number | undefined, from: number, to: number, durationMs: number): Promise<number> {
  const steps = Math.max(1, Math.min(200, Math.round(durationMs / 25)));
  for (let i = 0; i <= steps; i++) {
    const v = from + ((to - from) * i) / steps;
    const msg = messageFor(action, deck, v);
    bridge.send(msg.bytes);
    if (i < steps) await sleep(durationMs / steps);
  }
  return steps + 1;
}

/** Map a pitch percentage to the tempo fader value for a given pitch range (±8% by default). */
export function tempoValue(pct: number, rangePct = 8): number {
  const clamped = Math.max(-rangePct, Math.min(rangePct, pct));
  // 0 = -range, 64 = 0%, 127 = +range (the + side has one step fewer).
  const v = clamped >= 0 ? 64 + (clamped / rangePct) * 63 : 64 + (clamped / rangePct) * 64;
  return Math.max(0, Math.min(127, Math.round(v)));
}

export function mappingTable() {
  return Object.entries(ACTIONS).map(([name, d]) => ({
    action: name,
    type: d.kind === "button" ? "note" : "CC",
    channel: d.scope === "global" ? String(GLOBAL_CH) : "deck number (1-4)",
    number: d.number,
    description: d.description,
    mapTo: d.serato,
  }));
}
