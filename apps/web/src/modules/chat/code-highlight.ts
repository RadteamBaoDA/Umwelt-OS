/** Highlighter module, set once {@link loadHighlighter} resolves; until then fences render as escaped plain text. */
let engine: typeof import('./code-highlight-engine') | null = null;

/**
 * Lazily loads the highlight.js engine (kept out of the initial chat chunk).
 *
 * @returns Resolves once {@link highlightFence} can highlight; repeated calls reuse the loaded module.
 */
export async function loadHighlighter(): Promise<void> {
  if (!engine) engine = await import('./code-highlight-engine');
}

/**
 * Extracts a safe language label from a markdown fence info string.
 *
 * @param info - Raw info string after the opening fence (e.g. `ts title="x"`).
 * @returns The first word when it is a short identifier, else an empty string (no label rendered).
 */
export function fenceLanguage(info: string): string {
  const word = info.trim().split(/\s+/)[0] ?? '';
  return /^[\w+#.-]{1,24}$/.test(word) ? word : '';
}

/**
 * Highlights code for a fence once the engine is loaded (see {@link loadHighlighter}); the 20 000-char skip rule and
 * language checks live in the engine.
 *
 * @param code - Raw fence content.
 * @param language - Language word from {@link fenceLanguage}.
 * @returns Highlighted HTML, or null when the engine is not loaded yet, the language is unregistered or the code is too large.
 */
export function highlightFence(code: string, language: string): string | null {
  return engine ? engine.highlightFence(code, language) : null;
}
