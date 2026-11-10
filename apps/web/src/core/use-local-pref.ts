'use client';

import { useCallback, useState } from 'react';

/**
 * Small localStorage-backed preference restricted to an allow-list of string values.
 * The initial read is lazy and SSR-safe (falls back on the server): like the mobile-notice dismissal, it is used only
 * by client-rendered dashboard surfaces. Storage failures only cost persistence (the value still applies this page view).
 *
 * @param key localStorage key.
 * @param fallback Value used when nothing valid is stored.
 * @param allowed Accepted stored values; anything else is ignored.
 * @returns The current value and a setter that also persists it.
 */
export function useLocalPref<T extends string>(key: string, fallback: T, allowed: readonly T[]): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = typeof window === 'undefined' ? null : window.localStorage.getItem(key);
      return stored && (allowed as readonly string[]).includes(stored) ? (stored as T) : fallback;
    } catch { return fallback; }
  });
  /** Applies and persists a new value. */
  const set = useCallback((next: T) => {
    setValue(next);
    try { window.localStorage.setItem(key, next); } catch { /* storage unavailable */ }
  }, [key]);
  return [value, set];
}
