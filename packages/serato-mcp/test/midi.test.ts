import { test } from "node:test";
import assert from "node:assert/strict";
import midi from "@julusian/midi";
import { bridge, perform, sweep, PORT_NAME, tempoValue } from "../src/control/midi.js";

test("virtual MIDI device delivers deck messages (loopback)", async () => {
  bridge.open();
  await new Promise((r) => setTimeout(r, 300));
  const input = new midi.Input();
  const idx = [...Array(input.getPortCount()).keys()].find((i) => input.getPortName(i).includes(PORT_NAME));
  assert.notEqual(idx, undefined, `"${PORT_NAME}" should be visible to other MIDI apps`);
  const got: number[][] = [];
  input.on("message", (_d: number, m: number[]) => got.push(m));
  input.openPort(idx!);
  await perform("play_pause", 2);
  await perform("crossfader", undefined, 127);
  await sweep("filter", 1, 64, 0, 100);
  await new Promise((r) => setTimeout(r, 300));
  input.closePort();
  bridge.close();
  assert.deepEqual(got[0], [0x91, 0, 127]); // note on, channel 2
  assert.deepEqual(got[1], [0x81, 0, 0]); // note off
  assert.deepEqual(got[2], [0xbf, 8, 127]); // crossfader CC 8, channel 16
  assert.ok(got.slice(3).every((m) => m[0] === 0xb0 && m[1] === 19), "filter sweep on deck 1");
  assert.deepEqual(got[got.length - 1], [0xb0, 19, 0]);
  assert.equal(tempoValue(0), 64);
  assert.equal(tempoValue(8), 127);
  assert.equal(tempoValue(-8), 0);
  assert.equal(tempoValue(50, 8), 127);
});
