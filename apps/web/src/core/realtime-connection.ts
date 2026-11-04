import type { QueryClient } from '@tanstack/react-query';
import { ApiError, workspaceGeneration, workspaceHeaders } from '@/core/api';
import {
  CURSOR_RE,
  applyDashboardChange,
  applyIngestionChange,
  applyKnowledgeChange,
  applySourceChange,
  isCurrent,
  parseEnvelope,
  validCursor,
  validDashboardEvent,
  validFloorSequence,
  validIngestionEvent,
  validSourceEvent,
  type DashboardEvent,
  type IngestionEvent,
  type KnowledgeEvent,
  type SourceEvent,
} from '@/core/realtime-events';

export type RealtimeStatus = 'connecting' | 'connected' | 'reconnecting' | 'unavailable' | 'expired';

type ProtectedReadPath = '/api/v1/realtime/snapshot' | '/api/v1/auth/session';
type SnapshotAttempt = { id: number; controller: AbortController };
type Snapshot = { cursor: string; floor_sequence: string };

/** Marks a snapshot read that was canceled because a newer connection attempt took ownership. */
export class SupersededSnapshotAttempt extends Error {}

/** Returns the `&workspace_id=` suffix for the selected workspace (EventSource cannot send headers). */
function workspaceQuery(): string {
  const id = (workspaceHeaders('selected') as Record<string, string>)['X-Workspace-ID'];
  return id ? `&workspace_id=${encodeURIComponent(id)}` : '';
}

/** Extracts `error.message` from an unknown error body, or a generic message. */
function errorMessageOf(body: unknown): string {
  return body && typeof body === 'object'
    && 'error' in body && typeof body.error === 'object' && body.error
    && 'message' in body.error && typeof body.error.message === 'string'
    ? body.error.message : 'Request failed';
}

/** Reads protected JSON data and rejects stale attempts, unauthorized responses, and unsuccessful HTTP results. */
export async function protectedJsonRead<T>(
  path: ProtectedReadPath,
  signal: AbortSignal,
  isCurrentAttempt: () => boolean,
): Promise<T> {
  const response = await fetch(path, {
    cache: 'no-store', credentials: 'same-origin', signal,
    headers: path === '/api/v1/realtime/snapshot' ? workspaceHeaders('selected') : undefined,
  });
  if (!isCurrentAttempt()) throw new SupersededSnapshotAttempt();
  if (response.status === 401) throw new ApiError(401, 'Authentication required');
  let body: unknown;
  try {
    body = await response.json();
  } catch (error) {
    if (!response.ok) throw new ApiError(response.status, 'Request failed');
    throw error;
  }
  if (!isCurrentAttempt()) throw new SupersededSnapshotAttempt();
  if (!response.ok) throw new ApiError(response.status, errorMessageOf(body));
  return body as T;
}

/** Provider-side collaborators the connection drives; all callbacks must be safe to call after unmount. */
export interface RealtimeConnectionDeps {
  client: QueryClient;
  /** Auth-generation check from the display preferences (false once the user logged out/in again). */
  isCurrentAuthGeneration: () => boolean;
  setStatus: (status: RealtimeStatus) => void;
  /** Releases the render barrier for the connection's auth generation. */
  markBarrier: () => void;
  /** Last accepted event cursor, shared with the provider so it survives re-renders. */
  cursorRef: { current: string };
  /** Pending snapshot retry timer, shared with the provider. */
  retryRef: { current: ReturnType<typeof setTimeout> | null };
  invalidateOperationalQueries: () => Promise<void>;
  queueDocumentUpdate: (id: string | null) => void;
  /** Clears queued document state and counters when authentication ends. */
  resetDocumentState: () => void;
}

type StreamContext = { stream: EventSource; attempt: SnapshotAttempt };

/**
 * One authenticated realtime connection: snapshot, cursor-validated EventSource, retry/backoff and resync.
 * Everything is guarded by an ownership chain (active flag, auth generation, workspace generation, attempt token,
 * stream identity) so late callbacks from superseded connections are ignored. Not reusable after `dispose()`.
 */
export class RealtimeConnection {
  private active = true;
  private attemptSequence = 0;
  private currentAttempt: SnapshotAttempt | null = null;
  private eventSource: EventSource | null = null;
  private authCheckController: AbortController | null = null;
  private streamOpenTimer: ReturnType<typeof setTimeout> | null = null;
  private retryDelay = 1000;
  private lastAuthExpiredAt = 0;
  private readonly workspaceGen = workspaceGeneration();

  constructor(private readonly deps: RealtimeConnectionDeps) {}

  /** Starts the first snapshot attempt (not a reconnect). */
  start(): void {
    this.deps.setStatus('connecting');
    const initialAttempt = this.supersedeAttempt();
    void this.openSnapshot(false, initialAttempt, false);
  }

  /** Stops all work (attempt, stream, retry timer); used on effect cleanup. */
  dispose(): void {
    this.active = false;
    this.cancelAttempt();
    this.closeStream();
    this.clearSnapshotRetry();
  }

  /** Handles session expiration by ending authenticated client state and stopping the active stream. */
  authEnding = (): void => {
    this.dispose();
    this.deps.cursorRef.current = '';
    this.deps.resetDocumentState();
  };

  /** Checks whether asynchronous stream work still belongs to the active auth generation. */
  private isCurrentGeneration(): boolean {
    return this.active && this.deps.isCurrentAuthGeneration() && workspaceGeneration() === this.workspaceGen;
  }

  /** Checks that a snapshot attempt belongs to the active auth generation and has not been aborted. */
  private ownsAttempt(attempt: SnapshotAttempt): boolean {
    return this.isCurrentGeneration() && this.currentAttempt === attempt && !attempt.controller.signal.aborted;
  }

  /** Checks that the event stream and snapshot attempt still match the active connection. */
  private ownsStream(ctx: StreamContext): boolean {
    return this.eventSource === ctx.stream && this.ownsAttempt(ctx.attempt);
  }

  /** Aborts the prior snapshot and auth check before creating a new attempt token. */
  private supersedeAttempt(): SnapshotAttempt {
    this.currentAttempt?.controller.abort();
    this.authCheckController?.abort();
    this.authCheckController = null;
    const attempt = { id: ++this.attemptSequence, controller: new AbortController() };
    this.currentAttempt = attempt;
    return attempt;
  }

  /** Aborts and clears the active snapshot attempt and auth check. */
  private cancelAttempt(): void {
    this.currentAttempt?.controller.abort();
    this.currentAttempt = null;
    this.authCheckController?.abort();
    this.authCheckController = null;
  }

  /** Closes and clears the current event stream and its associated auth check. */
  private closeStream(): void {
    this.eventSource?.close();
    this.eventSource = null;
    this.clearOpenTimer();
    this.authCheckController?.abort();
    this.authCheckController = null;
  }

  /** Cancels the stream-open watchdog. */
  private clearOpenTimer(): void {
    if (this.streamOpenTimer) clearTimeout(this.streamOpenTimer);
    this.streamOpenTimer = null;
  }

  /** Cancels and clears the scheduled snapshot retry timer. */
  private clearSnapshotRetry(): void {
    if (this.deps.retryRef.current) clearTimeout(this.deps.retryRef.current);
    this.deps.retryRef.current = null;
  }

  /** Marks the session expired and notifies the shell so it returns to login. */
  private expire(): void {
    this.deps.setStatus('expired');
    window.dispatchEvent(new Event('umwelt:unauthorized'));
  }

  /** Fetches a session snapshot for the current stream attempt and reconciles its cursor before connecting. */
  private async openSnapshot(reconnect: boolean, attempt: SnapshotAttempt, possibleDocumentChanges: boolean): Promise<void> {
    if (!this.ownsAttempt(attempt)) return;
    try {
      const snapshot = await protectedJsonRead<Snapshot>(
        '/api/v1/realtime/snapshot', attempt.controller.signal, () => this.ownsAttempt(attempt),
      );
      if (!this.ownsAttempt(attempt)) return;
      if (!validCursor(snapshot.cursor) || !validFloorSequence(snapshot.floor_sequence)) {
        this.deps.setStatus('unavailable');
        this.scheduleSnapshotRetry();
        return;
      }
      if (reconnect) {
        await this.deps.invalidateOperationalQueries();
        if (!this.ownsAttempt(attempt)) return;
        if (possibleDocumentChanges) this.deps.queueDocumentUpdate(null);
      }
      if (!this.ownsAttempt(attempt)) return;
      this.openStream(snapshot.cursor, attempt);
      if (this.ownsAttempt(attempt)) this.deps.markBarrier();
    } catch (error) {
      this.handleSnapshotError(error, attempt);
    }
  }

  /** Maps a failed snapshot read to expired (401) or unavailable-with-retry; stale attempts are ignored. */
  private handleSnapshotError(error: unknown, attempt: SnapshotAttempt): void {
    if (!this.ownsAttempt(attempt) || error instanceof SupersededSnapshotAttempt) return;
    if (error instanceof ApiError && error.status === 401) {
      this.expire();
    } else {
      this.deps.setStatus('unavailable');
      this.deps.markBarrier();
      this.scheduleSnapshotRetry();
    }
  }

  /** Schedules a bounded-delay snapshot retry only while the current generation remains active. */
  private scheduleSnapshotRetry(): void {
    if (!this.isCurrentGeneration() || this.deps.retryRef.current) return;
    this.deps.retryRef.current = setTimeout(() => {
      this.deps.retryRef.current = null;
      const attempt = this.supersedeAttempt();
      void this.openSnapshot(true, attempt, true);
    }, this.retryDelay);
    this.retryDelay = Math.min(30_000, this.retryDelay * 2);
  }

  /** Replays events from a snapshot cursor and marks broad refresh when event changes cannot be targeted. */
  private resync(possibleDocumentChanges: boolean): void {
    const attempt = this.supersedeAttempt();
    this.closeStream();
    if (!this.ownsAttempt(attempt)) return;
    this.deps.setStatus('reconnecting');
    void this.openSnapshot(true, attempt, possibleDocumentChanges);
  }

  /** Installs one named event listener on the active stream, enforcing cursor ordering before the callback runs. */
  private register(ctx: StreamContext, name: string, callback: (event: MessageEvent<string>) => void): void {
    const { stream, attempt } = ctx;
    const { cursorRef } = this.deps;
    stream.addEventListener(name, (event) => {
      if (!(event instanceof MessageEvent) || this.eventSource !== stream || !this.ownsAttempt(attempt)) return;
      const id = event.lastEventId;
      if (!validCursor(id) || (cursorRef.current && !isCurrent(id, cursorRef.current))) {
        if (id && CURSOR_RE.exec(id)?.[1] !== CURSOR_RE.exec(cursorRef.current)?.[1]) this.resync(true);
        return;
      }
      callback(event as MessageEvent<string>);
      if (this.eventSource === stream && this.ownsAttempt(attempt)) cursorRef.current = id;
    });
  }

  /** Opens an event stream for the cursor and snapshot attempt after confirming that attempt still owns the connection. */
  private openStream(cursor: string, attempt: SnapshotAttempt): void {
    if (!this.ownsAttempt(attempt) || !validCursor(cursor)) return;
    this.closeStream();
    if (!this.ownsAttempt(attempt)) return;
    this.deps.cursorRef.current = cursor;
    const stream = new EventSource(`/api/v1/realtime/events?cursor=${encodeURIComponent(cursor)}${workspaceQuery()}`);
    this.eventSource = stream;
    const ctx: StreamContext = { stream, attempt };
    // UX rule: realtime never sits silently in 'connecting'. If the stream does not open (e.g. a proxy holds
    // headers), surface 'unavailable' and retry via a fresh snapshot instead.
    this.streamOpenTimer = setTimeout(() => {
      this.streamOpenTimer = null;
      if (!this.ownsStream(ctx)) return;
      this.closeStream();
      this.deps.setStatus('unavailable');
      this.scheduleSnapshotRetry();
    }, 20_000);
    stream.onopen = () => this.handleStreamOpen(ctx);
    stream.onerror = () => this.handleStreamError(ctx);
    this.registerDomainListeners(ctx);
    this.registerControlListeners(ctx);
  }

  /** Marks the stream connected, resets backoff and cancels any pending retry. */
  private handleStreamOpen(ctx: StreamContext): void {
    this.clearOpenTimer();
    if (!this.ownsStream(ctx)) return;
    this.retryDelay = 1000;
    this.clearSnapshotRetry();
    this.deps.setStatus('connected');
  }

  /** On stream error shows reconnecting and probes the session so a real 401 becomes `expired`. */
  private handleStreamError(ctx: StreamContext): void {
    this.clearOpenTimer();
    if (!this.ownsStream(ctx)) return;
    this.deps.setStatus('reconnecting');
    this.authCheckController?.abort();
    const controller = new AbortController();
    this.authCheckController = controller;
    /** Checks that the auth probe still belongs to the active stream and has not been aborted. */
    const ownsAuthCheck = () => this.ownsStream(ctx) && this.authCheckController === controller && !controller.signal.aborted;
    void protectedJsonRead<unknown>('/api/v1/auth/session', controller.signal, ownsAuthCheck)
      .catch((error) => {
        if (!ownsAuthCheck()) return;
        if (error instanceof ApiError && error.status === 401) this.expire();
      })
      .finally(() => {
        if (this.authCheckController === controller) this.authCheckController = null;
      });
  }

  /** Registers the domain event listeners; a malformed payload triggers a full resync. */
  private registerDomainListeners(ctx: StreamContext): void {
    const { client } = this.deps;
    this.register(ctx, 'source.changed', (event) => {
      const value = parseEnvelope<SourceEvent>(event.data);
      if (!validSourceEvent(value)) { this.resync(true); return; }
      applySourceChange(client, value);
    });
    this.register(ctx, 'ingestion.changed', (event) => {
      const value = parseEnvelope<IngestionEvent>(event.data);
      if (!validIngestionEvent(value)) { this.resync(true); return; }
      applyIngestionChange(client, value);
    });
    this.register(ctx, 'knowledge.changed', (event) => {
      const value = parseEnvelope<KnowledgeEvent>(event.data);
      if (!value || !applyKnowledgeChange(client, value, this.deps.queueDocumentUpdate)) this.resync(true);
    });
    this.register(ctx, 'dashboard.changed', (event) => {
      const value = parseEnvelope<DashboardEvent>(event.data);
      if (!validDashboardEvent(value)) { this.resync(true); return; }
      applyDashboardChange(client, value);
    });
  }

  /** Registers server control events (resync_required, auth_expired, connection_unavailable). */
  private registerControlListeners(ctx: StreamContext): void {
    const { stream } = ctx;
    stream.addEventListener('resync_required', () => { if (this.ownsStream(ctx)) this.resync(true); });
    stream.addEventListener('auth_expired', () => {
      if (!this.ownsStream(ctx)) return;
      // Password rotation also ends this stream's old session; the snapshot probe dispatches umwelt:unauthorized only on a real 401.
      // Repeated auth_expired within 5s is delayed so a misbehaving server cannot drive a tight resync loop.
      const wait = Date.now() - this.lastAuthExpiredAt < 5000 ? 5000 : 0;
      this.lastAuthExpiredAt = Date.now();
      if (wait) window.setTimeout(() => { if (this.ownsStream(ctx)) this.resync(true); }, wait);
      else this.resync(true);
    });
    stream.addEventListener('connection_unavailable', () => {
      if (!this.ownsStream(ctx)) return;
      this.closeStream();
      this.deps.setStatus('unavailable');
      this.scheduleSnapshotRetry();
    });
  }
}
