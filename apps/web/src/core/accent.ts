export const ACCENTS = ['ink', 'moss', 'sea', 'earth', 'plum'] as const;
export type AccentId = (typeof ACCENTS)[number];
export const ACCENT_STORAGE_KEY = 'umwelt-accent';

/**
 * Inline head script run before first paint so a saved accent does not flash the default.
 * Mirrors readAccent/previewAccent; keep in sync.
 */
export const ACCENT_INIT_SCRIPT = `try{var a=localStorage.getItem('${ACCENT_STORAGE_KEY}');if(a&&${JSON.stringify(ACCENTS.filter((id) => id !== 'ink'))}.indexOf(a)>-1)document.documentElement.setAttribute('data-accent',a)}catch(e){}`;

/** Returns the saved accent for this device, or `ink` when unset, invalid, or storage is unavailable. */
export function readAccent(): AccentId {
  try {
    const value = localStorage.getItem(ACCENT_STORAGE_KEY);
    return (ACCENTS as readonly string[]).includes(value ?? '') ? (value as AccentId) : 'ink';
  } catch {
    return 'ink';
  }
}

/**
 * Previews an accent on <html> without persisting it (used while the preferences dialog is open, so Cancel can revert).
 * `ink` removes the attribute.
 */
export function previewAccent(id: AccentId): void {
  const root = document.documentElement;
  if (id === 'ink') root.removeAttribute('data-accent'); else root.setAttribute('data-accent', id);
}

/**
 * Applies an accent to <html> and persists it. `ink` removes the attribute and the stored key.
 * ponytail: per-device (localStorage); move to owner preferences server-side if cross-device sync is wanted.
 */
export function applyAccent(id: AccentId): void {
  previewAccent(id);
  try {
    if (id === 'ink') localStorage.removeItem(ACCENT_STORAGE_KEY); else localStorage.setItem(ACCENT_STORAGE_KEY, id);
  } catch { /* storage blocked: the accent still applies for this page view */ }
}
