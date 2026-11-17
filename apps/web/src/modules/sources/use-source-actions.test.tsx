import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Source } from './api';
import { collectAndWait, pollCollectionRequest, useSourceActions, waitForActivation } from './use-source-actions';

const api = vi.hoisted(() => ({
  updateSourceStatus: vi.fn(),
  deactivateConnector: vi.fn(),
  archiveSource: vi.fn(),
  purgeSource: vi.fn(),
  triggerCollection: vi.fn(),
  getCollectionRequest: vi.fn(),
  getConnectorActivation: vi.fn(),
}));
const workspace = { csrfToken: 'csrf-1' };
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => workspace }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

const source = (overrides: Partial<Source> = {}) => ({ id: 's1', type: 'rss', status: 'active', ...overrides }) as Source;

function setup(props: Parameters<typeof useSourceActions>[0]) {
  const client = new QueryClient();
  const invalidate = vi.spyOn(client, 'invalidateQueries').mockResolvedValue();
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { invalidate, ...renderHook(() => useSourceActions(props), { wrapper }) };
}

describe('useSourceActions', () => {
  beforeEach(() => vi.clearAllMocks());

  it('resumes a paused source and reports the updated source', async () => {
    const resumed = source({ status: 'active' });
    api.updateSourceStatus.mockResolvedValue(resumed);
    const onResumed = vi.fn();
    const onChanged = vi.fn();
    const { result } = setup({ source: source({ status: 'paused' }), onResumed, onChanged });
    await act(() => result.current.toggleStatus());
    expect(api.updateSourceStatus).toHaveBeenCalledWith('s1', 'active', 'csrf-1');
    expect(onResumed).toHaveBeenCalledWith(resumed);
    expect(onChanged).not.toHaveBeenCalled();
    expect(result.current).toMatchObject({ busy: false, error: false });
  });

  it('deactivates an activated connector source instead of pausing it', async () => {
    api.deactivateConnector.mockResolvedValue({});
    const onChanged = vi.fn();
    const activation = { desired_revision: 2 } as never;
    const { result } = setup({ source: source(), activation, onChanged });
    await act(() => result.current.toggleStatus());
    expect(api.deactivateConnector).toHaveBeenCalledWith('s1', 'csrf-1');
    expect(api.updateSourceStatus).not.toHaveBeenCalled();
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it('pauses a non-connector source, and an unactivated connector too', async () => {
    api.updateSourceStatus.mockResolvedValue({});
    const first = setup({ source: source({ type: 'telegram' }), onChanged: vi.fn() });
    await act(() => first.result.current.toggleStatus());
    const second = setup({ source: source(), activation: { desired_revision: 0 } as never, onChanged: vi.fn() });
    await act(() => second.result.current.toggleStatus());
    expect(api.updateSourceStatus).toHaveBeenCalledTimes(2);
    expect(api.updateSourceStatus).toHaveBeenCalledWith('s1', 'paused', 'csrf-1');
    expect(api.deactivateConnector).not.toHaveBeenCalled();
  });

  it('sets error and clears busy when the status change fails; the next attempt clears the error', async () => {
    api.updateSourceStatus.mockRejectedValueOnce(new Error('x')).mockResolvedValueOnce({});
    const onChanged = vi.fn();
    const { result } = setup({ source: source(), onChanged });
    await act(() => result.current.toggleStatus());
    expect(result.current).toMatchObject({ busy: false, error: true });
    expect(onChanged).not.toHaveBeenCalled();
    await act(() => result.current.toggleStatus());
    expect(result.current.error).toBe(false);
  });

  it('disconnect archives by default and purges with the operation id when deleting data', async () => {
    api.archiveSource.mockResolvedValue({});
    api.purgeSource.mockResolvedValue({ operation_id: 'op-1' });
    const onChanged = vi.fn();
    const onPurgeStarted = vi.fn();
    const { result, invalidate } = setup({ source: source(), onChanged, onPurgeStarted });
    await act(() => result.current.disconnect(false));
    expect(api.archiveSource).toHaveBeenCalledWith('s1', 'csrf-1');
    expect(onPurgeStarted).not.toHaveBeenCalled();
    await act(() => result.current.disconnect(true));
    expect(api.purgeSource).toHaveBeenCalledWith('s1', 'csrf-1');
    expect(onPurgeStarted).toHaveBeenCalledWith('op-1');
    expect(onChanged).toHaveBeenCalledTimes(2);
    expect(invalidate).toHaveBeenCalled();
  });

  it('disconnect reports failure without calling onChanged', async () => {
    api.archiveSource.mockRejectedValue(new Error('x'));
    const onChanged = vi.fn();
    const { result } = setup({ source: source(), onChanged });
    await act(() => result.current.disconnect(false));
    expect(result.current.error).toBe(true);
    expect(onChanged).not.toHaveBeenCalled();
  });
});

describe('collection polling helpers', () => {
  beforeEach(() => vi.clearAllMocks());
  const request = (status: string, extra: Record<string, unknown> = {}) =>
    ({ request_id: 'r1', source_id: 's1', status, error_code: null, ingestion_run_id: null, ...extra }) as never;

  it('pollCollectionRequest polls until a terminal status, reporting each update', async () => {
    const fetchRequest = vi.fn().mockResolvedValueOnce(request('queued')).mockResolvedValueOnce(request('succeeded'));
    const onUpdate = vi.fn();
    const done = await pollCollectionRequest(fetchRequest, { signal: new AbortController().signal, intervalMs: 1, onUpdate });
    expect(done).toMatchObject({ status: 'succeeded' });
    expect(onUpdate).toHaveBeenCalledTimes(2);
  });

  it('pollCollectionRequest converts a timeout into a failed request and returns null when aborted', async () => {
    const stuck = vi.fn().mockResolvedValue(request('running'));
    const timedOut = await pollCollectionRequest(stuck, { signal: new AbortController().signal, intervalMs: 1, timeoutMs: 0 });
    expect(timedOut).toMatchObject({ status: 'failed', error_code: 'test_fetch_timeout' });
    const controller = new AbortController();
    controller.abort();
    expect(await pollCollectionRequest(stuck, { signal: controller.signal })).toBeNull();
  });

  it('waitForActivation resolves ok on active, reports error codes and times out', async () => {
    const signal = new AbortController().signal;
    api.getConnectorActivation.mockResolvedValueOnce({ state: 'pending' }).mockResolvedValueOnce({ state: 'active' });
    expect(await waitForActivation('s1', signal, { intervalMs: 1 })).toEqual({ ok: true, code: null });
    api.getConnectorActivation.mockResolvedValue({ state: 'saved_not_active', error_code: 'bad_key' });
    expect(await waitForActivation('s1', signal)).toEqual({ ok: false, code: 'bad_key' });
    api.getConnectorActivation.mockResolvedValue({ state: 'reconciliation_required', error_code: null });
    expect(await waitForActivation('s1', signal)).toEqual({ ok: false, code: 'reconciliation_required' });
    api.getConnectorActivation.mockResolvedValue({ state: 'pending' });
    expect(await waitForActivation('s1', signal, { timeoutMs: 0, intervalMs: 1 })).toEqual({ ok: false, code: 'activation_timeout' });
  });

  it('collectAndWait trusts a known terminal status without a request id and fails unknown ones', async () => {
    const signal = new AbortController().signal;
    api.triggerCollection.mockResolvedValueOnce({ request_id: null, status: 'no_changes', run_id: 'run-1' });
    expect(await collectAndWait('s1', 'csrf', { signal })).toMatchObject({
      status: 'no_changes',
      ingestion_run_id: 'run-1',
      error_code: null,
    });
    api.triggerCollection.mockResolvedValueOnce({ request_id: null, status: 'weird', run_id: null });
    expect(await collectAndWait('s1', 'csrf', { signal })).toMatchObject({ status: 'failed', error_code: 'no_request_id' });
  });

  it('collectAndWait polls the durable request when one is accepted', async () => {
    api.triggerCollection.mockResolvedValue({ request_id: 'r9', status: 'queued' });
    api.getCollectionRequest.mockResolvedValue(request('succeeded', { request_id: 'r9' }));
    const onAccepted = vi.fn();
    const done = await collectAndWait('s1', 'csrf', { signal: new AbortController().signal, intervalMs: 1, onAccepted });
    expect(onAccepted).toHaveBeenCalled();
    expect(api.getCollectionRequest).toHaveBeenCalledWith('s1', 'r9', expect.any(AbortSignal));
    expect(done).toMatchObject({ status: 'succeeded' });
  });
});
