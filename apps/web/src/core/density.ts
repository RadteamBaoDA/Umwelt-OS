export const DENSITIES = ['comfortable', 'compact'] as const;
export type DensityId = (typeof DENSITIES)[number];
export const DENSITY_STORAGE_KEY = 'umwelt-density';

/**
 * Inline head script run before first paint so a saved compact density does not flash comfortable.
 * Mirrors readDensity/applyDensity; keep in sync.
 */
export const DENSITY_INIT_SCRIPT = `try{if(localStorage.getItem('${DENSITY_STORAGE_KEY}')==='compact')document.documentElement.setAttribute('data-density','compact')}catch(e){}`;

/** Returns the density currently applied to <html> (`comfortable` when no attribute is set). */
export function readDensity(): DensityId {
  return document.documentElement.getAttribute('data-density') === 'compact' ? 'compact' : 'comfortable';
}

/** Applies a density to <html> and persists it; `comfortable` removes the attribute and the stored key. */
export function applyDensity(id: DensityId): void {
  const root = document.documentElement;
  if (id === 'compact') root.setAttribute('data-density', 'compact'); else root.removeAttribute('data-density');
  try {
    if (id === 'compact') localStorage.setItem(DENSITY_STORAGE_KEY, id); else localStorage.removeItem(DENSITY_STORAGE_KEY);
  } catch { /* storage blocked: the density still applies for this page view */ }
}
