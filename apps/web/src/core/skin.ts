export const SKINS = ['default', 'terminal'] as const;
export type SkinId = (typeof SKINS)[number];
export const SKIN_STORAGE_KEY = 'umwelt-skin';

/**
 * Inline head script run before first paint so a saved terminal skin does not flash the default.
 * Mirrors readSkin/applySkin; keep in sync. Only `terminal` is written; anything else leaves the default.
 */
export const SKIN_INIT_SCRIPT = `try{if(localStorage.getItem('${SKIN_STORAGE_KEY}')==='terminal')document.documentElement.setAttribute('data-skin','terminal')}catch(e){}`;

/**
 * Returns the skin currently applied to <html> (`default` when no attribute is set or during SSR).
 *
 * @returns `terminal` when `data-skin="terminal"` is present on the root element, otherwise `default`.
 */
export function readSkin(): SkinId {
  if (typeof document === 'undefined') return 'default';
  return document.documentElement.getAttribute('data-skin') === 'terminal' ? 'terminal' : 'default';
}

/**
 * Applies a skin to <html> and persists it on this device. `default` removes both the attribute and the stored key.
 * Storage failures (blocked or full) are swallowed, so the skin still applies for the current page view only.
 *
 * @param id Skin to apply.
 * @returns void. Side effects: mutates `data-skin` on `document.documentElement` and `localStorage`.
 */
export function applySkin(id: SkinId): void {
  const root = document.documentElement;
  if (id === 'terminal') root.setAttribute('data-skin', 'terminal');
  else root.removeAttribute('data-skin');
  try {
    if (id === 'terminal') localStorage.setItem(SKIN_STORAGE_KEY, id);
    else localStorage.removeItem(SKIN_STORAGE_KEY);
  } catch {
    /* storage blocked: the skin still applies for this page view */
  }
}
