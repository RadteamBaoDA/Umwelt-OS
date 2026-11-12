/// <reference lib="webworker" />
import { fetchSource, nextDelayMs, refreshable, type WorkerInbound, type WorkerOutbound } from './runner';
import { BROWSER_SOURCE_BY_ID, type LiveFeedScope } from './sources';
import { toStored, upsertEvents } from './store';

/**
 * Live-feed module worker. It never touches the DOM or app cookies: it fetches CORS-capable public sources
 * anonymously, writes events to the workspace IndexedDB and tells the page what changed. Polling runs only while the
 * page reports itself visible, so a hidden tab makes no background requests.
 */
const ctx = self as unknown as DedicatedWorkerGlobalScope;
let providers: string[] = [];
let scope: LiveFeedScope = {};
let workspaceId = '';
let visible = false;
let generation = 0;
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const failures = new Map<string, number>();
const inFlight = new Set<string>();
const lastRun = new Map<string, number>();

/** Post a typed message to the page. */
function post(message: WorkerOutbound): void {
  ctx.postMessage(message);
}

/** Cancel every pending timer and invalidate in-flight fetches so their results are discarded. */
function stopAll(): void {
  generation += 1;
  for (const timer of timers.values()) clearTimeout(timer);
  timers.clear();
}

/**
 * Fetch one provider, persist its events, report status, and schedule the next run with backoff.
 * A result is dropped when the config or visibility changed while it was in flight (`generation` mismatch).
 */
async function run(provider: string, runGeneration: number): Promise<void> {
  const source = BROWSER_SOURCE_BY_ID[provider];
  if (!source) return;
  let ok = true;
  let error: string | undefined;
  inFlight.add(provider);
  lastRun.set(provider, Date.now());
  try {
    const now = Date.now();
    const items = await fetchSource(provider, scope, now);
    if (runGeneration !== generation) return;
    const seenAt = new Date(now).toISOString();
    await upsertEvents(
      workspaceId,
      items.map((item) => toStored(provider, source.tags, item, seenAt)),
      now,
    );
    failures.set(provider, 0);
    post({ type: 'changed' });
  } catch (caught) {
    ok = false;
    error = caught instanceof Error ? caught.message : 'network';
    failures.set(provider, (failures.get(provider) ?? 0) + 1);
  } finally {
    inFlight.delete(provider);
  }
  if (runGeneration !== generation) return;
  post({ type: 'status', provider, ok, error, fetchedAt: new Date().toISOString() });
  // A missing scope will not fix itself between config messages, so it is not retried.
  if (error === 'scope') return;
  timers.set(
    provider,
    setTimeout(() => void run(provider, runGeneration), nextDelayMs(source.intervalMs, failures.get(provider) ?? 0)),
  );
}

/** (Re)start polling for the current config when visible; otherwise stay idle. */
function restart(): void {
  stopAll();
  if (!visible || !workspaceId) return;
  for (const provider of providers) void run(provider, generation);
}

ctx.onmessage = (message: MessageEvent<WorkerInbound>) => {
  const data = message.data;
  if (data.type === 'config') {
    providers = data.providers.filter((id) => id in BROWSER_SOURCE_BY_ID);
    scope = data.scope;
    workspaceId = data.workspaceId;
    failures.clear();
    restart();
  } else if (data.type === 'refresh') {
    // Manual refresh: fetch idle, not-recently-fetched providers now; their next run is rescheduled by run().
    if (!visible || !workspaceId) return;
    for (const provider of refreshable(providers, inFlight, lastRun, Date.now())) {
      clearTimeout(timers.get(provider));
      timers.delete(provider);
      failures.set(provider, 0);
      void run(provider, generation);
    }
  } else if (data.type === 'visibility' && data.visible !== visible) {
    visible = data.visible;
    restart();
  }
};
