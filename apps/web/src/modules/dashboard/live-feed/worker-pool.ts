import { registerLiveFeedTeardown } from '@/core/live-feed-storage';
import type { WorkerInbound, WorkerOutbound } from './runner';
import type { LiveFeedScope } from './sources';

/** Minimal worker surface the pool needs; the real `Worker` satisfies it. */
export type PoolWorker = {
  postMessage: (message: WorkerInbound) => void;
  terminate: () => void;
  onmessage: ((message: MessageEvent<WorkerOutbound>) => void) | null;
};

/** What one hook instance wants from the shared worker. */
export type Subscriber = {
  providers: string[];
  scope: LiveFeedScope;
  active: boolean;
  onMessage: (message: WorkerOutbound) => void;
};

/**
 * One pooled worker and its subscribers. `sentConfig` and `sentVisible` mirror what the worker was last told, so
 * `push` posts only real changes. The worker starts with `visible = false`, which `sentVisible` matches.
 */
type Entry = {
  worker: PoolWorker;
  subs: Set<Subscriber>;
  last: Map<string, WorkerOutbound>;
  onVisibility: () => void;
  sentConfig?: string;
  sentVisible: boolean;
};

const entries = new Map<string, Entry>();

/**
 * Merge every subscriber's wishes into one worker config: union of providers (stable order) and one scope. Visible
 * when any subscriber is active.
 *
 * Known limitation: the scope is the first subscriber's that names a GitHub repo, because browser sources read only
 * `live_map_github_owner`/`live_map_github_repo`. Two subscribers in one workspace that want different repos share
 * the first repo, so the second one's `github_releases` rows come from the first repo. Per-provider scopes would need
 * a per-provider field in the worker config message; not implemented.
 * @returns Providers, scope and the active flag to post to the worker.
 */
export function unionConfig(subs: Iterable<Subscriber>): { providers: string[]; scope: LiveFeedScope; active: boolean } {
  const providers = new Set<string>();
  let scope: LiveFeedScope = {};
  let active = false;
  for (const sub of subs) {
    sub.providers.forEach((id) => providers.add(id));
    if (!scope.live_map_github_repo && sub.scope.live_map_github_repo) scope = sub.scope;
    active ||= sub.active;
  }
  return { providers: [...providers].sort(), scope, active };
}

/**
 * Serialize the config a worker would receive into a comparison key. Scope keys are sorted so equal scopes built in
 * a different field order still match; providers are already sorted by `unionConfig`.
 * @returns A string that is equal for equal configs.
 */
function configKey(workspaceId: string, providers: string[], scope: LiveFeedScope): string {
  const sortedScope = Object.fromEntries(Object.entries(scope).sort(([a], [b]) => a.localeCompare(b)));
  return JSON.stringify({ workspaceId, providers, scope: sortedScope });
}

/**
 * Bring the entry's worker in line with the merged subscriber wishes. Posts `config` only when it differs from the last
 * one sent: a config message restarts the worker's polling, clears its failure counts and drops in-flight results, so
 * re-posting an unchanged config would reset 429 backoff for no reason. `visibility` is posted only when it flips.
 */
function push(workspaceId: string, entry: Entry): void {
  const { providers, scope, active } = unionConfig(entry.subs);
  const key = configKey(workspaceId, providers, scope);
  if (key !== entry.sentConfig) {
    entry.sentConfig = key;
    entry.worker.postMessage({ type: 'config', providers, scope, workspaceId });
  }
  const visible = active && document.visibilityState === 'visible';
  if (visible !== entry.sentVisible) {
    entry.sentVisible = visible;
    entry.worker.postMessage({ type: 'visibility', visible });
  }
}

/** Cache the latest status/items per provider so a late subscriber can be caught up. */
function remember(entry: Entry, message: WorkerOutbound): void {
  if (message.type === 'changed') return;
  entry.last.set(`${message.type}:${message.provider}`, message);
  if (message.type === 'status' && message.ok) entry.last.delete(`items:${message.provider}`);
}

/**
 * Subscribe to the one worker shared by all hook instances of a workspace (created on first subscriber, terminated
 * when the last releases). Messages reach a subscriber only for providers it asked for; the latest per-provider
 * state is replayed to late joiners.
 * @param create Factory for the worker, called only for the first subscriber of a workspace.
 * @returns `update` to change this subscriber's wishes, `refresh` to ask the worker to fetch now, `release` to leave.
 */
export function subscribeWorker(workspaceId: string, sub: Subscriber, create: () => PoolWorker) {
  let entry = entries.get(workspaceId);
  if (!entry) {
    const created: Entry = {
      worker: create(),
      subs: new Set(),
      last: new Map(),
      onVisibility: () => push(workspaceId, created),
      sentVisible: false,
    };
    created.worker.onmessage = (message) => {
      remember(created, message.data);
      for (const s of created.subs)
        if (message.data.type === 'changed' || s.providers.includes(message.data.provider)) s.onMessage(message.data);
    };
    document.addEventListener('visibilitychange', created.onVisibility);
    entries.set(workspaceId, created);
    entry = created;
  }
  const joined = entry;
  joined.subs.add(sub);
  push(workspaceId, joined);
  for (const message of joined.last.values())
    if (sub.providers.includes((message as { provider: string }).provider)) sub.onMessage(message);
  return {
    /**
     * Replace this subscriber's providers, scope and activity, then push the merged config. A no-op after release,
     * so a stale hook cannot re-post to a terminated worker.
     */
    update(next: Pick<Subscriber, 'providers' | 'scope' | 'active'>) {
      if (!joined.subs.has(sub)) return;
      Object.assign(sub, next);
      push(workspaceId, joined);
    },
    /** Ask the shared worker to fetch now (it rate-limits repeated requests itself). */
    refresh() {
      joined.worker.postMessage({ type: 'refresh' });
    },
    /**
     * Leave; the last subscriber terminates the worker and drops the visibility listener. Idempotent: a repeated
     * release returns early, so it cannot terminate or unmap a newer worker for the same workspace.
     */
    release() {
      if (!joined.subs.delete(sub)) return;
      if (joined.subs.size) return push(workspaceId, joined);
      document.removeEventListener('visibilitychange', joined.onVisibility);
      joined.worker.terminate();
      // Only unmap this entry: a newer entry may already occupy the workspace slot.
      if (entries.get(workspaceId) === joined) entries.delete(workspaceId);
    },
  };
}

/**
 * Terminate every pooled worker, drop its visibility listener and forget all subscribers. Called before the
 * live-feed IndexedDB is deleted on logout and account erasure: a live worker could otherwise reopen the database
 * after the delete and recreate it. Subscriptions left behind become inert (`release`/`update` no-op).
 */
export function terminateAll(): void {
  for (const entry of entries.values()) {
    document.removeEventListener('visibilitychange', entry.onVisibility);
    entry.worker.terminate();
    entry.subs.clear();
  }
  entries.clear();
}

// Registered at module load so core's teardown reaches live workers without importing this feature module.
registerLiveFeedTeardown(terminateAll);
