import * as React from 'react';

/** A user-saved widget template: the sandbox payload serialized as JSON plus display metadata. */
export interface UserTemplate {
  /** Stable local identifier. */
  id: string;
  /** Display title (the widget title when available). */
  title: string;
  /** JSON-serialized sandbox payload (html/css/js fields) captured from a rendered widget. */
  content: string;
  /** Epoch milliseconds when the template was saved. */
  createdAt: number;
}

const STORAGE_KEY = 'umwelt.genui.templates.v1';
/** Per-template and total-count caps keep localStorage use bounded (~5 MB browser quota). */
export const MAX_TEMPLATE_CHARS = 60_000;
export const MAX_TEMPLATES = 30;

const listeners = new Set<() => void>();
let cachedRaw: string | null | undefined;
let cachedList: UserTemplate[] = [];
const EMPTY: UserTemplate[] = [];

/** Reads the raw stored string; storage may be blocked, so any failure reads as empty. */
function readRaw(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Type guard dropping malformed entries so corrupted storage can never crash the drawer. */
function isTemplate(value: unknown): value is UserTemplate {
  const v = value as Partial<UserTemplate> | null;
  return !!v && typeof v.id === 'string' && typeof v.title === 'string'
    && typeof v.content === 'string' && typeof v.createdAt === 'number';
}

/**
 * Returns the saved templates, newest first. The result is referentially stable until storage
 * changes so it is safe as a `useSyncExternalStore` snapshot.
 *
 * @returns Parsed, validated template list (empty when storage is unavailable or corrupt).
 */
export function listUserTemplates(): UserTemplate[] {
  if (typeof window === 'undefined') return EMPTY;
  const raw = readRaw();
  if (raw === cachedRaw) return cachedList;
  cachedRaw = raw;
  try {
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    cachedList = Array.isArray(parsed) ? parsed.filter(isTemplate) : EMPTY;
  } catch {
    cachedList = EMPTY;
  }
  return cachedList;
}

/** Persists the list and notifies subscribers; returns false when storage rejects the write. */
function persist(list: UserTemplate[]): boolean {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
  } catch {
    return false;
  }
  listeners.forEach((l) => l());
  return true;
}

/**
 * Saves a new template at the front of the list.
 *
 * @param title - Display title.
 * @param content - JSON-serialized sandbox payload; rejected when longer than MAX_TEMPLATE_CHARS.
 * @returns True when stored; false when oversize or browser storage is unavailable/full.
 */
export function saveUserTemplate(title: string, content: string): boolean {
  if (!content || content.length > MAX_TEMPLATE_CHARS) return false;
  const entry: UserTemplate = {
    id: `tpl-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
    title: title.trim().slice(0, 120) || 'Untitled',
    content,
    createdAt: Date.now(),
  };
  return persist([entry, ...listUserTemplates()].slice(0, MAX_TEMPLATES));
}

/**
 * Deletes a saved template by id (no-op for unknown ids).
 *
 * @param id - Template identifier.
 * @returns True when the resulting list was stored.
 */
export function deleteUserTemplate(id: string): boolean {
  return persist(listUserTemplates().filter((t) => t.id !== id));
}

/** Subscribes to same-tab writes and cross-tab `storage` events. */
function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  window.addEventListener('storage', listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', listener);
  };
}

/**
 * React hook exposing the saved templates and keeping them in sync across tabs.
 *
 * ponytail: browser-local only (single device); server persistence per user is a follow-up
 * once a templates API exists.
 *
 * @returns Current saved templates, newest first.
 */
export function useUserTemplates(): UserTemplate[] {
  return React.useSyncExternalStore(subscribe, listUserTemplates, () => EMPTY);
}
