import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { apiRequest, configureWorkspace, StaleWorkspaceError } from '@/core/api';

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
