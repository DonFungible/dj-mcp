/**
 * Serato DJ Pro application control on macOS: launch, quit, focus, and
 * keyboard-driven library search / track loading via System Events.
 * Keyboard control needs Accessibility permission for the app running the MCP
 * client (System Settings > Privacy & Security > Accessibility).
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { seratoRunning } from "../serato/writer.js";

const run = promisify(execFile);
export const APP_NAME = process.env.SERATO_APP_NAME ?? "Serato DJ Pro";

async function osascript(script: string): Promise<string> {
  try {
    const { stdout } = await run("osascript", ["-e", script], { timeout: 20000 });
    return stdout.trim();
  } catch (err: any) {
    const msg = String(err.stderr || err.message);
    if (/not allowed|assistive|1002|-25211|-1719/i.test(msg)) {
      throw new Error(
        "macOS blocked keyboard control. Give the app running this MCP client (Terminal, Claude, Cursor...) Accessibility access in System Settings > Privacy & Security > Accessibility, then retry."
      );
    }
    throw new Error(`AppleScript failed: ${msg.trim()}`);
  }
}

const esc = (s: string) => s.replace(/\\/g, "\\\\").replace(/"/g, '\\"');

export async function launch(): Promise<string> {
  if (seratoRunning()) return `${APP_NAME} is already running`;
  await run("open", ["-a", APP_NAME]);
  return `Launching ${APP_NAME}`;
}

export async function quit(): Promise<string> {
  if (!seratoRunning()) return `${APP_NAME} is not running`;
  await osascript(`tell application "${esc(APP_NAME)}" to quit`);
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 500));
    if (!seratoRunning()) return `${APP_NAME} quit`;
  }
  return `Asked ${APP_NAME} to quit, but it is still running (it may be showing a dialog)`;
}

export async function focus(): Promise<void> {
  if (!seratoRunning()) throw new Error(`${APP_NAME} is not running. Use launch_serato first.`);
  await osascript(`tell application "${esc(APP_NAME)}" to activate`);
}

const KEY_CODES: Record<string, number> = { left: 123, right: 124, down: 125, up: 126, return: 36, enter: 76, tab: 48, escape: 53, space: 49, delete: 51 };

/** Send one keystroke to Serato, e.g. key "f" with ["command"], or "left" with ["shift"]. */
export async function keystroke(key: string, modifiers: string[] = []): Promise<void> {
  await focus();
  const using = modifiers.length ? ` using {${modifiers.map((m) => `${m} down`).join(", ")}}` : "";
  const code = KEY_CODES[key.toLowerCase()];
  const action = code !== undefined ? `key code ${code}` : `keystroke "${esc(key)}"`;
  await osascript(`tell application "System Events" to tell process "${esc(APP_NAME)}" to ${action}${using}`);
}

/** Type a query into Serato's library search box (Cmd+F) and leave focus on the results. */
export async function search(query: string): Promise<void> {
  await focus();
  await osascript(`
    tell application "System Events" to tell process "${esc(APP_NAME)}"
      keystroke "f" using {command down}
      delay 0.15
      keystroke "a" using {command down}
      key code 51
      keystroke "${esc(query)}"
      delay 0.6
      key code 36
    end tell`);
}

/**
 * Load the highlighted track to the left (deck 1) or right (deck 2) deck with
 * Serato's default Shift+Left / Shift+Right shortcuts.
 */
export async function loadSelected(deck: 1 | 2): Promise<void> {
  await keystroke(deck === 1 ? "left" : "right", ["shift"]);
}
