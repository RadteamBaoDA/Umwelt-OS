import * as React from 'react';
import { apiRequest, csrfHeaders } from '@/core/api';
import { useWorkspace } from '@/core/workspace-context';

/** A user-saved widget template: the sandbox payload serialized as JSON plus display metadata. */
export interface UserTemplate {
  /** Server UUID, or a `tpl-` local id while the entry has not reached the server yet. */
  id: string;
  /** Display title (the widget title when available). */
  title: string;
  /** JSON-serialized sandbox payload (html/css/js fields) captured from a rendered widget. */
  content: string;
  /** Epoch milliseconds when the template was saved. */
  createdAt: number;
}

/** Server projection of a saved template (`TemplateRead`). */
interface ServerTemplate {
  id: string;
  name: string;
  html: string;
  created_at: string;
}

/**
 * The browser mirror is namespaced per account (`<prefix><accountId>`) so account B never sees or uploads
 * account A's entries on a shared browser. The pre-namespacing key has no owner, so its entries are
 * dropped (never POSTed to the server for an unknown account); the drop is silent.
 */
const KEY_PREFIX = 'umwelt.genui.templates.v2:';
const LEGACY_KEY = 'umwelt.genui.templates.v1';
/** Account owning the mirror right now; null until the workspace list resolves (store is then empty/read-only). */
let accountId: string | null = null;
const API = '/api/v1/chat/templates';
/** Per-template and total-count caps (mirrored by the server) keep storage use bounded. */
export const MAX_TEMPLATE_CHARS = 60_000;
export const MAX_TEMPLATES = 30;
/** Ids minted in the browser start with `tpl-`; server rows use UUIDs, so this marks not-yet-synced entries. */
const LOCAL_PREFIX = 'tpl-';
/** Local ids with a POST currently in flight, so a refresh never migrates (and duplicates) them. */
const inflight = new Set<string>();

const listeners = new Set<() => void>();
let cachedRaw: string | null | undefined;
let cachedList: UserTemplate[] = [];
const EMPTY: UserTemplate[] = [];

/** Reads the raw stored string; storage may be blocked, so any failure reads as empty. */
function readRaw(): string | null {
  if (!accountId) return null;
  try {
    return window.localStorage.getItem(KEY_PREFIX + accountId);
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
 * changes so it is safe as a `useSyncExternalStore` snapshot. The browser copy mirrors the server
 * list after each refresh and is the read-through fallback when the API is unreachable.
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
  if (!accountId) return false;
  try {
    window.localStorage.setItem(KEY_PREFIX + accountId, JSON.stringify(list));
  } catch {
    return false;
  }
  listeners.forEach((l) => l());
  return true;
}

/** Maps a server row onto the local shape (name to title, html to content). */
function fromServer(row: ServerTemplate): UserTemplate {
  const createdAt = Date.parse(row.created_at);
  return { id: row.id, title: row.name, content: row.html, createdAt: Number.isNaN(createdAt) ? Date.now() : createdAt };
}

/**
 * POSTs one local entry and swaps it for the server row. A failure leaves it unsynced; the next refresh retries.
 *
 * @param entry - Local entry to upload.
 * @param csrfToken - Session CSRF token.
 * @returns True when the server stored it.
 */
async function syncOne(entry: UserTemplate, csrfToken: string): Promise<boolean> {
  const owner = accountId;
  if (!owner) return false;
  inflight.add(entry.id);
  try {
    const row = await apiRequest<ServerTemplate>(API, {
      method: 'POST',
      headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: entry.title, html: entry.content }),
    });
    // Account switched/logged out mid-flight: the mirror now belongs to someone else, leave it alone.
    if (owner !== accountId) return false;
    persist(listUserTemplates().map((t) => (t.id === entry.id ? fromServer(row) : t)));
    return true;
  } catch {
    return false;
  } finally {
    inflight.delete(entry.id);
  }
}

/**
 * Saves a new template at the front of the list.
 *
 * The entry is written locally first (instant UI, offline fallback) and, when a CSRF token is supplied,
 * mirrored to the server in the background; the local id is swapped for the server id on success and kept
 * as an unsynced entry on failure so the next refresh migrates it.
 *
 * @param title - Display title.
 * @param content - JSON-serialized sandbox payload; rejected when longer than MAX_TEMPLATE_CHARS.
 * @param csrfToken - Session CSRF token enabling the server mirror; omit for browser-only storage.
 * @returns True when stored locally; false when oversize or browser storage is unavailable/full.
 */
export function saveUserTemplate(title: string, content: string, csrfToken?: string): boolean {
  if (!content || content.length > MAX_TEMPLATE_CHARS) return false;
  const entry: UserTemplate = {
    id: `${LOCAL_PREFIX}${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
    title: title.trim().slice(0, 120) || 'Untitled',
    content,
    createdAt: Date.now(),
  };
  const stored = persist([entry, ...listUserTemplates()].slice(0, MAX_TEMPLATES));
  if (stored && csrfToken) void syncOne(entry, csrfToken);
  return stored;
}

/**
 * Loads the server list and merges it with the browser copy. Idempotent one-time migration: any entry that
 * still has a local id (saved before the API existed, or while it was unreachable) is POSTed once and replaced
 * by its server row; entries that fail to upload stay local. When the API is unreachable the browser copy
 * (the read-through fallback) is left untouched.
 *
 * @param csrfToken - Session CSRF token for the migration writes.
 * @returns Resolves when the merge finished (never rejects).
 */
export async function refreshUserTemplates(csrfToken: string): Promise<void> {
  const owner = accountId;
  if (!owner) return;
  try {
    await apiRequest<ServerTemplate[]>(API); // reachability probe: nothing is migrated against a dead API
    for (const entry of [...listUserTemplates()].reverse()) {
      if (owner !== accountId) return;
      if (entry.id.startsWith(LOCAL_PREFIX) && !inflight.has(entry.id)) await syncOne(entry, csrfToken);
    }
    const server = await apiRequest<ServerTemplate[]>(API);
    if (owner !== accountId) return;
    const unsynced = listUserTemplates().filter((t) => t.id.startsWith(LOCAL_PREFIX));
    persist([...unsynced, ...server.map(fromServer)].slice(0, MAX_TEMPLATES));
  } catch {
    /* API unreachable or signed out: keep serving the browser copy */
  }
}

/**
 * Marks a template as the conversation's pending template so the model's `apply_template` consumes it.
 *
 * @param conversationId - Existing conversation id.
 * @param templateId - Server template UUID or built-in seed id.
 * @param csrfToken - Session CSRF token.
 * @returns True when the server accepted it; false (the caller falls back to a prompt) on any failure.
 */
export async function setPendingTemplate(conversationId: string, templateId: string, csrfToken: string): Promise<boolean> {
  try {
    await apiRequest(`/api/v1/conversations/${encodeURIComponent(conversationId)}/pending-template`, {
      method: 'PUT',
      headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
      body: JSON.stringify({ template_id: templateId }),
    });
    return true;
  } catch {
    return false;
  }
}

/**
 * Deletes a saved template by id (no-op for unknown ids).
 *
 * @param id - Template identifier.
 * @param csrfToken - Session CSRF token; a server-backed template is also deleted remotely in the background.
 * @returns True when the resulting list was stored.
 */
export function deleteUserTemplate(id: string, csrfToken?: string): boolean {
  const stored = persist(listUserTemplates().filter((t) => t.id !== id));
  if (csrfToken && !id.startsWith(LOCAL_PREFIX)) {
    void apiRequest(`${API}/${encodeURIComponent(id)}`, { method: 'DELETE', headers: csrfHeaders(csrfToken) })
      .catch(() => undefined);
  }
  return stored;
}

/**
 * Switches the mirror to another account (or none). Cached reads are invalidated and subscribers notified;
 * the legacy un-namespaced key is removed because its owner is unknown.
 *
 * @param next - Stable account id, or null when signed out / not yet resolved.
 */
export function setTemplateAccount(next: string | null): void {
  if (typeof window === 'undefined' || next === accountId) return;
  accountId = next;
  cachedRaw = undefined;
  try {
    window.localStorage.removeItem(LEGACY_KEY);
  } catch {
    /* storage blocked: nothing to drop */
  }
  listeners.forEach((l) => l());
}

/**
 * Removes the current account's mirror and detaches it (logout / session expiry).
 * Unsynced `tpl-` entries are discarded with it; the server remains the source of truth.
 */
export function clearUserTemplates(): void {
  if (typeof window === 'undefined') return;
  const owner = accountId;
  accountId = null;
  cachedRaw = undefined;
  try {
    if (owner) window.localStorage.removeItem(KEY_PREFIX + owner);
  } catch {
    /* storage blocked */
  }
  listeners.forEach((l) => l());
}

/**
 * Binds the store to the signed-in account (the default workspace's owner) and clears it on logout.
 * Outside a workspace provider (or before the list loads) the store stays detached and empty.
 */
export function useTemplateAccount(): void {
  let owner: string | null = null;
  try {
    // useContext is unconditional inside; the catch only maps "no provider" to null.
    const ws = useWorkspace().workspaces.find((w) => w.is_default);
    owner = ws ? String(ws.owner_user_id) : null;
  } catch {
    owner = null;
  }
  React.useEffect(() => {
    setTemplateAccount(owner);
  }, [owner]);
  React.useEffect(() => {
    window.addEventListener('bbd:auth-ending', clearUserTemplates);
    return () => window.removeEventListener('bbd:auth-ending', clearUserTemplates);
  }, []);
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
 * With a CSRF token (signed-in chat) it refreshes from the server once on mount, migrating browser-only
 * entries; without one it stays browser-local.
 *
 * @param csrfToken - Session CSRF token enabling server sync.
 * @returns Current saved templates, newest first.
 */
export function useUserTemplates(csrfToken?: string): UserTemplate[] {
  useTemplateAccount();
  const account = React.useSyncExternalStore(subscribe, () => accountId, () => null);
  React.useEffect(() => {
    if (csrfToken && account) void refreshUserTemplates(csrfToken);
  }, [csrfToken, account]);
  return React.useSyncExternalStore(subscribe, listUserTemplates, () => EMPTY);
}
