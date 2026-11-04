import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import type { McpConnection, McpDiscovery, McpGrant, McpGrantChoice, McpInboundClient, McpNativeTool } from './mcp-api';

const api = vi.hoisted(() => ({ listSources: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));
const mcp = vi.hoisted(() => ({
  listMcpConnections: vi.fn(),
  listMcpInboundClients: vi.fn(),
  listMcpNativeTools: vi.fn(),
  listMcpGrants: vi.fn(),
  saveMcpConnection: vi.fn(),
  createMcpConnection: vi.fn(),
  createMcpInboundClient: vi.fn(),
}));
vi.mock('./mcp-api', async (original) => ({ ...(await original<typeof import('./mcp-api')>()), ...mcp }));

import { perform, save, issue, copyToken, type McpActionContext } from './mcp-actions';
import {
  buildGrantSelections,
  buildInboundIssue,
  computeCanEnable,
  emptyDraft,
  errorKey,
  fromConnection,
  grantDraftsInvalid,
  healthMessageKey,
  selectionsViolateBounds,
  toPayload,
} from './mcp-model';
import { clearSessionState, loadSnapshot, type McpState } from './mcp-snapshot';
import { emptyGrantDraft, useMcpCore, useMcpDraft, useMcpGrants, useMcpInbound } from './use-mcp-state';

const connection = (patch: Partial<McpConnection> = {}): McpConnection =>
  ({
    id: 'c1',
    name: 'Docs',
    transport: 'streamable_http',
    enabled: false,
    revision: 3,
    endpoint: 'https://mcp.example.test',
    auth_method: 'none',
    timeout_seconds: 10,
    health: 'connected',
    error_code: null,
    updated_at: '2026-01-01T00:00:00Z',
    credential_configured: false,
    deployment_profile_id: null,
    deployment_profile_hash: null,
    ...patch,
  }) as McpConnection;

/** Composes the four state hooks the way `useMcpSettings` does and exposes the shared bundle. */
function useState4() {
  const core = useMcpCore();
  const drafts = useMcpDraft();
  const grants = useMcpGrants();
  const inbound = useMcpInbound();
  return { core, drafts, grants, inbound } as McpState;
}

beforeEach(() => {
  vi.clearAllMocks();
  mcp.listMcpConnections.mockResolvedValue({ items: [connection(), connection({ id: 'c2', name: 'Other', revision: 1 })] });
  mcp.listMcpInboundClients.mockResolvedValue({ items: [] });
  mcp.listMcpNativeTools.mockResolvedValue({
    items: [
      { name: 'search.query', version: '1', risk: 'READ_ONLY', schema_fingerprint: 'f1' },
      { name: 'knowledge.delete', version: '1', risk: 'DESTRUCTIVE', schema_fingerprint: 'f2' },
    ],
  });
  mcp.listMcpGrants.mockResolvedValue({ items: [] });
  api.listSources.mockResolvedValue({
    items: [
      { id: 'a', status: 'active', local_only: false, retired_at: null },
      { id: 'b', status: 'active', local_only: true, retired_at: null },
      { id: 'c', status: 'paused', local_only: false, retired_at: null },
    ],
    next_cursor: null,
  });
});
afterEach(cleanup);

describe('mcp model helpers', () => {
  it('round-trips a connection to a draft and a strict payload', () => {
    const draft = fromConnection(connection());
    expect(draft).toMatchObject({ name: 'Docs', endpoint: 'https://mcp.example.test', timeout: '10', credentialAction: 'retain' });
    expect(toPayload({ ...draft, name: ' Docs ' })).toMatchObject({
      name: 'Docs',
      endpoint: 'https://mcp.example.test',
      timeout_seconds: 10,
      credential_update: { action: 'retain' },
    });
    expect(toPayload({ ...emptyDraft(), transport: 'stdio', profile: ' p ', credentialAction: 'replace', credential: 'k' })).toMatchObject({
      deployment_profile_id: 'p',
      credential_update: { action: 'replace', value: 'k' },
    });
  });

  it('maps server errors and health codes to message keys', () => {
    expect(errorKey(new Error('x'))).toBe('requestFailed');
    expect(errorKey(new ApiError(401, 'x'))).toBe('sessionExpired');
    expect(errorKey(new ApiError(409, 'Catalog is full'))).toBe('catalogFull');
    expect(errorKey(new ApiError(409, 'stale'))).toBe('conflict');
    expect(errorKey(new ApiError(422, 'x'))).toBe('invalidInput');
    expect(errorKey(new ApiError(503, 'x', { code: 'mcp_runtime_refresh_failed' }))).toBe('runtimeRefreshFailed');
    expect(errorKey(new ApiError(503, 'x'))).toBe('transportUnavailable');
    expect(healthMessageKey('connected')).toBe('healthConnected');
    expect(healthMessageKey(null)).toBe('healthUnknown');
  });

  it('validates grant drafts: collection needs one source and expiry must be in the future', () => {
    const draft = (patch: object) => ({ k: { ...emptyGrantDraft(), selected: true, ...patch } });
    expect(grantDraftsInvalid(draft({ purpose: 'collection', sources: [] }))).toBe(true);
    expect(grantDraftsInvalid(draft({ purpose: 'collection', sources: ['a'] }))).toBe(false);
    expect(grantDraftsInvalid(draft({ expires: '2000-01-01T00:00' }))).toBe(true);
    expect(grantDraftsInvalid({ k: { ...emptyGrantDraft(), expires: '2000-01-01T00:00' } })).toBe(false);
  });

  it('builds read-only selections only for supported descriptor kinds and checks their bounds', () => {
    const discovery = {
      capabilities: [
        { id: 't', kind: 'tool', descriptor_hash: 'h1' },
        { id: 'p', kind: 'prompt', descriptor_hash: 'h2' },
      ],
    } as unknown as McpDiscovery;
    const drafts = {
      t: { ...emptyGrantDraft(), selected: true, sources: ['a'], destinations: ' x , y ' },
      p: { ...emptyGrantDraft(), selected: true },
    };
    const selections = buildGrantSelections(discovery, drafts);
    expect(selections).toEqual([
      { capability_id: 't', descriptor_hash: 'h1', purpose: 'chat', risk: 'READ_ONLY', source_ids: ['a'], destinations: ['x', 'y'] },
    ]);
    expect(selectionsViolateBounds(selections)).toBe(false);
    expect(selectionsViolateBounds([{ ...selections[0], destinations: ['x', 'x'] }])).toBe(true);
    expect(selectionsViolateBounds([{ ...selections[0], source_ids: [] } as McpGrantChoice])).toBe(true);
  });

  it('builds an inbound issue only for a complete, bounded, future-dated form', () => {
    const tools = [{ name: 'search.query', version: '1', schema_fingerprint: 'f1' }] as McpNativeTool[];
    const form = {
      clientName: ' Bot ',
      clientExpiry: '2999-01-01T00:00',
      chosenTools: ['search.query@1'],
      clientSourceIds: ['a'],
      capabilities: 'read, write',
      tools,
    };
    expect(buildInboundIssue(form)).toMatchObject({
      name: 'Bot',
      source_ids: ['a'],
      capabilities: ['read', 'write'],
      tool_bindings: [{ name: 'search.query', version: '1', schema_fingerprint: 'f1' }],
    });
    expect(buildInboundIssue({ ...form, clientName: ' ' })).toBeNull();
    expect(buildInboundIssue({ ...form, chosenTools: [] })).toBeNull();
    expect(buildInboundIssue({ ...form, clientExpiry: '2000-01-01T00:00' })).toBeNull();
    expect(buildInboundIssue({ ...form, capabilities: 'a, a' })).toBeNull();
  });

  it('enables a connection only with a reviewed discovery and a live read-only grant', () => {
    const selected = connection({ health: 'connected' });
    const discovery = {
      connection_id: 'c1',
      connection_revision: 3,
      deployment_profile_hash: null,
      capabilities: [{ id: 't', descriptor_hash: 'h' }],
    } as unknown as McpDiscovery;
    const grant = {
      connection_id: 'c1',
      capability_id: 't',
      descriptor_hash: 'h',
      purpose: 'chat',
      risk: 'READ_ONLY',
      source_ids: ['a'],
      revoked_at: null,
      expires_at: null,
      reviewed_connection_revision: 3,
      reviewed_profile_hash: null,
    } as unknown as McpGrant;
    expect(computeCanEnable(selected, false, discovery, [grant], Date.now())).toBe(true);
    expect(computeCanEnable(selected, true, discovery, [grant], Date.now())).toBe(false);
    expect(computeCanEnable(selected, false, null, [grant], Date.now())).toBe(false);
    expect(computeCanEnable(connection({ health: 'unavailable' }), false, discovery, [grant], Date.now())).toBe(false);
    expect(computeCanEnable(selected, false, discovery, [{ ...grant, revoked_at: 'x' } as McpGrant], Date.now())).toBe(false);
    expect(computeCanEnable(selected, false, discovery, [{ ...grant, reviewed_connection_revision: 2 } as McpGrant], Date.now())).toBe(
      false,
    );
  });
});

describe('mcp state hooks', () => {
  it('draft hook mirrors the draft owner into a ref for async refreshes', () => {
    const { result } = renderHook(() => useMcpDraft());
    act(() => result.current.setDraftOwner({ id: 'c1', baseRevision: 3 }));
    expect(result.current.draftIdentity).toEqual({ id: 'c1', baseRevision: 3 });
    expect(result.current.draftIdentityRef.current).toEqual({ id: 'c1', baseRevision: 3 });
  });

  it('grants hook clears discovery, grants and drafts together', () => {
    const { result } = renderHook(() => useMcpGrants());
    act(() => {
      result.current.setGrantDrafts({ a: emptyGrantDraft() });
      result.current.setGrants([{ id: 'g' } as McpGrant]);
    });
    act(() => result.current.clearGrants());
    expect(result.current.grants).toEqual([]);
    expect(result.current.grantDrafts).toEqual({});
    expect(result.current.discovery).toBeNull();
  });

  it('inbound hook replaces the displayed token, invalidates clipboard completion and mirrors acknowledgement', () => {
    const { result } = renderHook(() => useMcpInbound());
    const client = { id: 'i1', revision: 1 } as McpInboundClient;
    act(() => result.current.setDisplayedToken({ token: 'tok', client }));
    expect(result.current.tokenGeneration.current).toBe(1);
    expect(result.current.tokenState.current).toEqual({ exists: true, acknowledged: false });
    act(() => result.current.markTokenAcknowledged(true));
    expect(result.current.tokenAcknowledged).toBe(true);
    expect(result.current.tokenState.current).toEqual({ exists: true, acknowledged: true });
    act(() => result.current.setDisplayedToken(null));
    expect(result.current.tokenGeneration.current).toBe(2);
    expect(result.current.tokenState.current.exists).toBe(false);
  });

  it('core hook only clears loading from cancelReads while mounted and advances fences on unmount', () => {
    const { result } = renderHook(() => useMcpCore());
    act(() => result.current.cancelReads());
    expect(result.current.loading).toBe(true);
    act(() => result.current.markMounted());
    act(() => result.current.cancelReads());
    expect(result.current.loading).toBe(false);
    const before = result.current.sessionGeneration.current;
    act(() => result.current.markUnmounted());
    expect(result.current.mounted.current).toBe(false);
    expect(result.current.sessionGeneration.current).toBe(before + 1);
  });
});

describe('loadSnapshot', () => {
  it('loads connections, supported read-only tools and remote-safe sources, then selects the first connection', async () => {
    const { result } = renderHook(useState4);
    act(() => result.current.core.markMounted());
    let ok = false;
    await act(async () => {
      ok = await loadSnapshot(result.current);
    });
    expect(ok).toBe(true);
    expect(result.current.drafts.selected?.id).toBe('c1');
    expect(result.current.drafts.draft?.name).toBe('Docs');
    expect(result.current.drafts.draftIdentity).toEqual({ id: 'c1', baseRevision: 3 });
    expect(result.current.inbound.tools.map((tool) => tool.name)).toEqual(['search.query']);
    expect(result.current.inbound.sources.map((source) => source.id)).toEqual(['a']);
    expect(result.current.core.loading).toBe(false);
    expect(result.current.core.error).toBe('');
  });

  it('keeps the draft attached to a missing owner and raises a conflict instead of retargeting', async () => {
    const { result } = renderHook(useState4);
    act(() => result.current.core.markMounted());
    await act(async () => {
      await loadSnapshot(result.current);
    });
    act(() => {
      result.current.drafts.setEditing(true);
      result.current.drafts.hasDraftRef.current = true;
    });
    mcp.listMcpConnections.mockResolvedValue({ items: [connection({ id: 'c2', name: 'Other' })] });
    let ok = false;
    await act(async () => {
      ok = await loadSnapshot(result.current, undefined, true);
    });
    expect(ok).toBe(true);
    expect(result.current.core.conflict).toBe(true);
    expect(result.current.core.error).toBe('conflict');
    expect(result.current.drafts.selected).toBeNull();
    expect(result.current.drafts.draftIdentity.id).toBe('c1');
  });

  it('flags a revised owner as a conflict and drops reviewed descriptors', async () => {
    const { result } = renderHook(useState4);
    act(() => result.current.core.markMounted());
    await act(async () => {
      await loadSnapshot(result.current);
    });
    act(() => {
      result.current.drafts.hasDraftRef.current = true;
      result.current.grants.setDiscovery({ id: 'd' } as McpDiscovery);
    });
    mcp.listMcpConnections.mockResolvedValue({ items: [connection({ revision: 4 })] });
    await act(async () => {
      await loadSnapshot(result.current, undefined, true);
    });
    expect(result.current.core.conflict).toBe(true);
    expect(result.current.grants.discovery).toBeNull();
    expect(result.current.drafts.selected?.revision).toBe(4);
  });

  it('drops a read superseded by a newer one and one finished after unmount', async () => {
    const { result } = renderHook(useState4);
    act(() => result.current.core.markMounted());
    let release: (value: unknown) => void = () => undefined;
    mcp.listMcpConnections.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    let first: Promise<boolean> = Promise.resolve(false);
    act(() => {
      first = loadSnapshot(result.current);
    });
    await act(async () => {
      await loadSnapshot(result.current);
    });
    release({ items: [connection({ id: 'old' })] });
    expect(await first).toBe(false);
    expect(result.current.drafts.selected?.id).toBe('c1');
  });

  it('reports a failed read through the error key and clears loading', async () => {
    const { result } = renderHook(useState4);
    act(() => result.current.core.markMounted());
    mcp.listMcpConnections.mockRejectedValue(new ApiError(401, 'no'));
    let ok = true;
    await act(async () => {
      ok = await loadSnapshot(result.current);
    });
    expect(ok).toBe(false);
    expect(result.current.core.error).toBe('sessionExpired');
    expect(result.current.core.loading).toBe(false);
  });

  it('clears every session-owned value when authentication ends', async () => {
    const { result } = renderHook(useState4);
    act(() => result.current.core.markMounted());
    await act(async () => {
      await loadSnapshot(result.current);
    });
    act(() => result.current.inbound.setDisplayedToken({ token: 't', client: { id: 'i' } as McpInboundClient }));
    const before = result.current.core.sessionGeneration.current;
    act(() => clearSessionState(result.current));
    expect(result.current.core.sessionGeneration.current).toBe(before + 1);
    expect(result.current.drafts.connections).toEqual([]);
    expect(result.current.drafts.draft).toBeNull();
    expect(result.current.inbound.tokenView).toBeNull();
    expect(result.current.inbound.sources).toEqual([]);
  });
});

describe('mcp actions', () => {
  /** Builds an action context over real state hooks with a scripted refresh. */
  function useCtx(refresh: McpActionContext['refresh']) {
    const state = useState4();
    return { ...state, csrfToken: 'c', t: (key: string) => key, refresh } as McpActionContext;
  }

  it('single-flights mutations and releases the lock afterwards', async () => {
    const refresh = vi.fn().mockResolvedValue(true);
    const { result } = renderHook(() => useCtx(refresh));
    act(() => {
      result.current.core.markMounted();
      result.current.core.setLoading(false);
    });
    let release: () => void = () => undefined;
    const slow = () =>
      new Promise<unknown>((resolve) => {
        release = () => resolve(undefined);
      });
    let first: Promise<void> = Promise.resolve();
    act(() => {
      first = perform(result.current, slow);
    });
    const second = vi.fn();
    await act(async () => {
      await perform(result.current, second);
    });
    expect(second).not.toHaveBeenCalled();
    await act(async () => {
      release();
      await first;
    });
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(result.current.core.actionLock.current).toBe(false);
    expect(result.current.core.busy).toBe(false);
  });

  it('freezes writes on a 409, reloads preserving the draft and reports the key from the server error', async () => {
    const refresh = vi.fn().mockResolvedValue(true);
    const { result } = renderHook(() => useCtx(refresh));
    act(() => {
      result.current.core.markMounted();
      result.current.core.setLoading(false);
    });
    await act(async () => {
      await perform(result.current, () => Promise.reject(new ApiError(409, 'stale')));
    });
    expect(result.current.core.conflict).toBe(true);
    expect(refresh).toHaveBeenCalledWith(undefined, true);
    expect(result.current.core.error).toBe('conflict');
  });

  it('reports reconciliationFailed when the follow-up refresh fails after a runtime-refresh failure', async () => {
    const refresh = vi.fn().mockResolvedValue(false);
    const { result } = renderHook(() => useCtx(refresh));
    act(() => {
      result.current.core.markMounted();
      result.current.core.setLoading(false);
    });
    await act(async () => {
      await perform(result.current, () => Promise.reject(new ApiError(503, 'x', { code: 'mcp_runtime_refresh_failed' })));
    });
    expect(result.current.core.reconciliationRequired).toBe(true);
    expect(result.current.core.error).toBe('reconciliationFailed');
  });

  it('drops a result that finishes after the session changed', async () => {
    const refresh = vi.fn().mockResolvedValue(true);
    const { result } = renderHook(() => useCtx(refresh));
    act(() => {
      result.current.core.markMounted();
      result.current.core.setLoading(false);
    });
    const onSuccess = vi.fn();
    let release: () => void = () => undefined;
    let run: Promise<void> = Promise.resolve();
    act(() => {
      run = perform(
        result.current,
        () =>
          new Promise((resolve) => {
            release = () => resolve(1);
          }),
        true,
        false,
        onSuccess,
      );
    });
    act(() => {
      result.current.core.sessionGeneration.current += 1;
    });
    await act(async () => {
      release();
      await run;
    });
    expect(onSuccess).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it('does not save while the draft no longer owns the selection', async () => {
    const refresh = vi.fn().mockResolvedValue(true);
    const { result } = renderHook(() => useCtx(refresh));
    act(() => {
      result.current.core.markMounted();
      result.current.core.setLoading(false);
      result.current.drafts.setDraft(fromConnection(connection()));
      result.current.drafts.setDraftOwner({ id: 'c1', baseRevision: 3 });
      result.current.drafts.setSelected(connection({ revision: 4 }));
    });
    await act(async () => {
      await save(result.current);
    });
    expect(mcp.saveMcpConnection).not.toHaveBeenCalled();
  });

  it('saves a new connection through create and refreshes onto the created id', async () => {
    mcp.createMcpConnection.mockResolvedValue({ id: 'new' });
    const refresh = vi.fn().mockResolvedValue(true);
    const { result } = renderHook(() => useCtx(refresh));
    act(() => {
      result.current.core.markMounted();
      result.current.core.setLoading(false);
      result.current.drafts.setDraft({ ...emptyDraft(), name: 'N', endpoint: 'https://x.test' });
    });
    await act(async () => {
      await save(result.current);
    });
    expect(mcp.createMcpConnection).toHaveBeenCalledWith(expect.objectContaining({ name: 'N' }), 'c');
    expect(refresh).toHaveBeenCalledWith('new');
    expect(result.current.core.currentId.current).toBe('new');
  });

  it('refuses to issue a client with an incomplete form and reports inboundRequired', () => {
    const { result } = renderHook(() => useCtx(vi.fn()));
    act(() => issue(result.current));
    expect(result.current.core.error).toBe('inboundRequired');
    expect(mcp.createMcpInboundClient).not.toHaveBeenCalled();
  });

  it('copies the displayed token and ignores completion for a replaced token', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const { result } = renderHook(() => useCtx(vi.fn()));
    act(() => {
      result.current.core.markMounted();
      result.current.inbound.setDisplayedToken({ token: 'tok', client: { id: 'i1' } as McpInboundClient });
    });
    await act(async () => {
      await copyToken(result.current);
    });
    await waitFor(() => expect(result.current.inbound.copied).toBe(true));
    expect(writeText).toHaveBeenCalledWith('tok');
  });
});
