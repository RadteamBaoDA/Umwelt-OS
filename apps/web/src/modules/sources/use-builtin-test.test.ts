import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useBuiltinTest } from './use-builtin-test';

const actions = vi.hoisted(() => ({ waitForActivation: vi.fn(), collectAndWait: vi.fn() }));
vi.mock('./use-source-actions', () => actions);

describe('useBuiltinTest', () => {
  beforeEach(() => { actions.waitForActivation.mockReset(); actions.collectAndWait.mockReset(); });

  it('records success and notifies the caller', async () => {
    actions.waitForActivation.mockResolvedValue({ ok: true, code: null });
    actions.collectAndWait.mockResolvedValue({ status: 'succeeded', ingestion_run_id: 'run-1', error_code: null });
    const settled = vi.fn();
    const { result } = renderHook(() => useBuiltinTest('csrf', settled));
    expect(result.current.test.phase).toBe('idle');
    await act(async () => { await result.current.runTest('s1'); });
    expect(result.current.test).toEqual({ phase: 'succeeded', runId: 'run-1' });
    expect(result.current.verified).toBe(true);
    expect(result.current.testing).toBe(false);
    expect(settled).toHaveBeenCalledTimes(1);
  });

  it('fails with the activation code and does not collect', async () => {
    actions.waitForActivation.mockResolvedValue({ ok: false, code: 'credential_missing' });
    const { result } = renderHook(() => useBuiltinTest('csrf', vi.fn()));
    await act(async () => { await result.current.runTest('s1'); });
    expect(result.current.test).toEqual({ phase: 'failed', code: 'credential_missing' });
    expect(actions.collectAndWait).not.toHaveBeenCalled();
  });

  it('maps a non-success run status to a failed code and unknown throws to request_failed', async () => {
    actions.waitForActivation.mockResolvedValue({ ok: true, code: null });
    actions.collectAndWait.mockResolvedValueOnce({ status: 'failed', ingestion_run_id: null, error_code: null });
    const { result } = renderHook(() => useBuiltinTest('csrf', vi.fn()));
    await act(async () => { await result.current.runTest('s1'); });
    expect(result.current.test).toEqual({ phase: 'failed', code: 'failed' });
    actions.collectAndWait.mockRejectedValueOnce(new Error('boom'));
    await act(async () => { await result.current.runTest('s1'); });
    await waitFor(() => expect(result.current.test).toEqual({ phase: 'failed', code: 'request_failed' }));
  });

  it('ignores a run superseded by abort', async () => {
    let release: (value: unknown) => void = () => undefined;
    actions.waitForActivation.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const { result } = renderHook(() => useBuiltinTest('csrf', vi.fn()));
    let pending: Promise<void> = Promise.resolve();
    act(() => { pending = result.current.runTest('s1'); });
    expect(result.current.testing).toBe(true);
    act(() => result.current.abort());
    await act(async () => { release({ ok: true, code: null }); await pending; });
    expect(actions.collectAndWait).not.toHaveBeenCalled();
  });
});
