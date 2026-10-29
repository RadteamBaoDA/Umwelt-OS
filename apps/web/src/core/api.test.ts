import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, apiRequest, configureWorkspace, StaleWorkspaceError } from '@/core/api';

const fetchMock = vi.fn();
let generation = 1;
const abort = new AbortController();

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function sentHeaders(callIndex = 0): Headers {
  const [, init] = fetchMock.mock.calls[callIndex] as [string, RequestInit];
  return new Headers(init.headers);
}

beforeEach(() => {
  generation = 1;
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
  vi.stubGlobal('fetch', fetchMock);
  configureWorkspace({
    selectedId: () => 'ws-1',
    generation: () => generation,
    signal: () => abort.signal,
  });
});

afterEach(() => {
  configureWorkspace(null);
  vi.unstubAllGlobals();
});

describe('apiRequest workspace scoping', () => {
  it('sends X-Workspace-ID on a selected-workspace path', async () => {
    await apiRequest('/api/v1/documents');

    expect(sentHeaders().get('X-Workspace-ID')).toBe('ws-1');
  });

  it.each(['/api/v1/workspaces', '/api/v1/exports?format=csv'])(
    'omits X-Workspace-ID on the default-workspace path %s',
    async (path) => {
      await apiRequest(path);

      expect(sentHeaders().has('X-Workspace-ID')).toBe(false);
    },
  );

  it('rejects with StaleWorkspaceError when the workspace generation changes mid-flight', async () => {
    fetchMock.mockImplementation(async () => {
      generation += 1;
      return jsonResponse({ ok: true });
    });

    await expect(apiRequest('/api/v1/documents')).rejects.toBeInstanceOf(StaleWorkspaceError);
  });
});

describe('non-JSON responses', () => {
  it('throws ApiError with the gateway status instead of a parse error', async () => {
    fetchMock.mockResolvedValue(new Response('<html>Bad Gateway</html>', { status: 502 }));
    await expect(apiRequest('/api/v1/x')).rejects.toMatchObject({ status: 502 });
    fetchMock.mockResolvedValue(new Response('<html>Bad Gateway</html>', { status: 502 }));
    await expect(apiRequest('/api/v1/x')).rejects.toBeInstanceOf(ApiError);
  });
});

describe('retrySessionConflict', () => {
  it('retries only a 409 ApiError, at most 3 times', async () => {
    const { ApiError, retrySessionConflict } = await import('./api');
    const conflict = new ApiError(409, 'Authentication state changed; retry the request');
    expect(retrySessionConflict(0, conflict)).toBe(true);
    expect(retrySessionConflict(2, conflict)).toBe(true);
    expect(retrySessionConflict(3, conflict)).toBe(false);
    expect(retrySessionConflict(0, new ApiError(500, 'boom'))).toBe(false);
    expect(retrySessionConflict(0, new Error('x'))).toBe(false);
  });
});
