/**
 * Terminal-skin palette for the generative-UI sandbox (`sandbox/terminal-palette.ts`).
 * The sandbox is a separate document with its own built-in dark and light token sets, so the host's terminal colours
 * do not reach it on their own. This module reads the host's computed semantic custom properties (no hex copies) and
 * returns them as `SandboxPalette` overrides for `buildSandboxStylesheet`.
 */

import { OPEN_INTELLIGENT_UI_TOKEN_ALIASES, UMWELT_SEMANTIC_TOKEN_NAMES, type SandboxPalette } from '../design-system';

/**
 * Mono font stack for the sandbox's sans and mono variables under the terminal skin. It mirrors the fallback chain of
 * `body` in `globals.css`. The JetBrains face is not passed on purpose: the frame CSP has `font-src data:` only, so a
 * web font from the host could never load in the frame and the local monospace fallbacks are what render.
 */
export const TERMINAL_MONO_FONT_STACK = `'JetBrains Mono', 'Cascadia Code', ui-monospace, Consolas, monospace`;

/** Characters a host colour or length can contain. Excludes `;`, braces, `<`, `>`, quotes and backslashes, so a value cannot end the declaration or the rule. */
const SAFE_CSS_VALUE = /^[#\w\s%(),./-]+$/;

/**
 * Checks that a value read from the host can be written into the sandbox stylesheet unchanged.
 *
 * @param value - Trimmed computed custom property value.
 * @returns True when the value uses only plain colour or length characters and cannot load a URL.
 */
function isSafeCssValue(value: string): boolean {
  return SAFE_CSS_VALUE.test(value) && !/url\(/i.test(value);
}

/**
 * Builds the terminal palette from the host document. Reads each semantic token (`--surface`, `--text`, ...) as a
 * computed custom property so the terminal values stay defined in `globals.css` only, then adds the derived colour
 * aliases (which reference those semantic names inside the sandbox) and the mono font stack.
 *
 * Only the terminal skin produces entries: any other skin, or a missing root, returns `{}` so the sandbox keeps its
 * built-in palette. A semantic token that is undefined or unsafe is left out and the sandbox default applies to it.
 *
 * @param root - The host `<html>` element (`document.documentElement`), or null when there is no document.
 * @returns Palette overrides keyed by CSS custom property name; empty outside the terminal skin.
 */
export function resolveTerminalPalette(root: HTMLElement | null): SandboxPalette {
  if (!root || root.getAttribute('data-skin') !== 'terminal') return {};
  const computed = getComputedStyle(root);
  const palette: Record<string, string> = {};
  for (const name of UMWELT_SEMANTIC_TOKEN_NAMES) {
    const value = computed.getPropertyValue(`--${name}`).trim();
    if (value && isSafeCssValue(value)) palette[`--${name}`] = value;
  }
  // Aliases are CSS expressions over the semantic names, so they resolve to the terminal values inside the frame.
  Object.assign(palette, OPEN_INTELLIGENT_UI_TOKEN_ALIASES);
  palette['--font-sans'] = TERMINAL_MONO_FONT_STACK;
  palette['--font-mono'] = TERMINAL_MONO_FONT_STACK;
  return palette;
}
