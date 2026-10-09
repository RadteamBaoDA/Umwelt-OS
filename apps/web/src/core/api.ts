/** Represents an unsuccessful API response with its HTTP status and optional machine-readable error details. */
export class ApiError extends Error {
  readonly code: string | null;
  readonly details: unknown;
  /** Full error object (e.g. top-level `document_ids` on a 409), not only the recognized fields. */
  readonly payload: Record<string, unknown>;
  /** Copies the response status, message, and recognized payload fields onto the error. */
  constructor(
    readonly status: number,
    message: string,
    payload?: { code?: unknown; details?: unknown; [key: string]: unknown },
  ) {
    super(message);
    this.code = typeof payload?.code === 'string' ? payload.code : null;
    this.details = payload?.details;
    this.payload = payload ?? {};
  }
}

/** Thrown when the selected workspace changed while a request was in flight; callers must drop the result. */
export class StaleWorkspaceError extends Error {
  constructor() { super('Workspace changed before the response resolved'); this.name = 'StaleWorkspaceError'; }
}

export type WorkspaceTarget = 'selected' | 'none';
type WorkspaceSource = { selectedId(): string | null; generation(): number; signal(): AbortSignal };
let workspaceSource: WorkspaceSource | null = null;

/** Installs (or clears with null) the active workspace source used by every API request. */
export function configureWorkspace(source: WorkspaceSource | null): void { workspaceSource = source; }
/** Returns the active workspace generation, or 0 before a source is configured. */
export function workspaceGeneration(): number { return workspaceSource?.generation() ?? 0; }

// Default-workspace routes accept a missing header for every account; sending another workspace would 409.
const NO_WORKSPACE_PREFIXES = [
  '/api/v1/auth', '/api/v1/workspaces', '/api/v1/chat', '/api/v1/memory', '/api/v1/agents',
  '/api/v1/agent-runs', '/api/v1/approvals', '/api/v1/exports', '/api/v1/system', '/api/v1/backup',
];

/** Classifies an API path as workspace-scoped ('selected') or default-workspace ('none'). */
export function workspaceTargetFor(path: string): WorkspaceTarget {
  const bare = path.split(/[?#]/, 1)[0];
  return NO_WORKSPACE_PREFIXES.some((prefix) => bare.startsWith(prefix))
    ? 'none' : 'selected';
}

/** Builds the X-Workspace-ID header for the selected workspace; empty for 'none' or no selection. */
export function workspaceHeaders(target: WorkspaceTarget = 'selected'): HeadersInit {
  const id = target === 'selected' ? workspaceSource?.selectedId() : null;
  return id ? { 'X-Workspace-ID': id } : {};
}

type Session = { authenticated: true; csrfToken: string };
let sessionRefresh: Promise<Session> | null = null;

/** Sends a same-origin API request, applies the provided request options, and rejects unsuccessful responses as ApiError. */
export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  retryCsrf = true,
): Promise<T> {
  const headers = new Headers(options.headers);
  const scoped = workspaceTargetFor(path) === 'selected';
  const source = scoped ? workspaceSource : null;
  const generation = source?.generation() ?? 0;
  const isStale = () => source !== null && source.generation() !== generation;
  if (scoped && !headers.has('X-Workspace-ID')) {
    new Headers(workspaceHeaders('selected')).forEach((value, key) => headers.set(key, value));
  }
  // Chain the per-generation abort signal with any caller signal.
  const signals = [options.signal, source?.signal()].filter((s): s is AbortSignal => Boolean(s));
  const signal = signals.length > 1 ? AbortSignal.any(signals) : signals[0];
  let response: Response;
  try {
    response = await fetch(path, {
      ...options,
      cache: 'no-store',
      credentials: 'same-origin',
      headers,
      signal,
    });
  } catch (error) {
    if (isStale()) throw new StaleWorkspaceError();
    throw error;
  }
  if (isStale()) throw new StaleWorkspaceError();
  if (
    retryCsrf && response.status === 403 &&
    response.headers.get('X-CSRF-Error') === 'invalid' &&
    headers.has('X-CSRF-Token') &&
    !['GET', 'HEAD'].includes((options.method ?? 'GET').toUpperCase())
  ) {
    // Share one session refresh across concurrent rejected mutations to avoid competing CSRF rotations.
    if (isStale()) throw new StaleWorkspaceError();
    sessionRefresh ??= apiRequest<Session>('/api/v1/auth/session')
      .then((session) => {
        window.dispatchEvent(new CustomEvent('bbd:session-refreshed', { detail: session }));
        return session;
      })
      .finally(() => { sessionRefresh = null; });
    const session = await sessionRefresh;
    headers.set('X-CSRF-Token', session.csrfToken);
    // Disable further CSRF retries so a failed refresh cannot recurse indefinitely.
    return apiRequest<T>(path, { ...options, headers }, false);
  }
  if (response.status === 204) return undefined as T;

  const body = (await response.json()) as { error?: { message?: string; code?: string; details?: unknown; [key: string]: unknown }; detail?: string | { message?: string; code?: string; details?: unknown; [key: string]: unknown } } & T;
  if (isStale()) throw new StaleWorkspaceError();
  if (!response.ok) {
    if (scoped && typeof window !== 'undefined' && response.status === 404 && body.detail === 'Workspace not found') {
      // The selected workspace itself is gone or revoked: let the provider fall back to the default.
      window.dispatchEvent(new Event('bbd:workspace-invalid'));
    }
    if (response.status === 401 && typeof window !== 'undefined') {
      window.dispatchEvent(new Event('bbd:unauthorized'));
    }
    const detail = typeof body.detail === 'object' && body.detail !== null ? body.detail : undefined;
    const message = body.error?.message ?? detail?.message ?? (typeof body.detail === 'string' ? body.detail : 'Request failed');
    throw new ApiError(response.status, message, body.error ?? detail);
  }
  return body;
}

/** Builds the request headers used to submit the supplied CSRF token. */
export function csrfHeaders(token: string): HeadersInit {
  return { 'X-CSRF-Token': token };
}
