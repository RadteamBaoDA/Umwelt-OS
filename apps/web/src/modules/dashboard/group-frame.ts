import { LAYOUT_FRAMES, type LayoutFrame } from './layout-geometry';

const KEY_PREFIX = 'umwelt.dashboard.groupFrame.';

/**
 * Reads the layout frame chosen for a custom page; `free` when none is stored or storage fails.
 * Read at call time (not a hook) because the key depends on the active group.
 */
export function getGroupFrame(groupId: string): LayoutFrame {
  try {
    const stored = typeof window === 'undefined' ? null : window.localStorage.getItem(KEY_PREFIX + groupId);
    return (LAYOUT_FRAMES as readonly string[]).includes(stored ?? '') ? (stored as LayoutFrame) : 'free';
  } catch { return 'free'; }
}

/**
 * Stores the layout frame of a page.
 * ponytail: frame stored per browser; add a dashboard_groups.frame column if cross-device sync is needed
 */
export function setGroupFrame(groupId: string, frame: LayoutFrame): void {
  try { window.localStorage.setItem(KEY_PREFIX + groupId, frame); } catch { /* storage unavailable: frame lasts nowhere */ }
}
