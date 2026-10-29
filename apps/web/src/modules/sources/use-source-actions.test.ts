import { describe, expect, it, vi } from 'vitest';
import type { CollectionRequest, CollectionStatus } from './api';
import { pollCollectionRequest } from './use-source-actions';

const req = (status: CollectionStatus): CollectionRequest => ({ request_id: 'r', source_id: 's', status, ingestion_run_id: null, error_code: null });

describe('pollCollectionRequest', () => {
  it('polls until a terminal status and reports each update', async () => {
    const fetchRequest = vi.fn()
      .mockResolvedValueOnce(req('queued')).mockResolvedValueOnce(req('running')).mockResolvedValueOnce(req('succeeded'));
    const seen: string[] = [];
    const result = await pollCollectionRequest(fetchRequest, { signal: new AbortController().signal, intervalMs: 1, onUpdate: (r) => seen.push(r.status) });
    expect(result?.status).toBe('succeeded');
    expect(fetchRequest).toHaveBeenCalledTimes(3);
    expect(seen).toEqual(['queued', 'running', 'succeeded']);
  });

  it('settles as failed when a retrying request outlives timeoutMs', async () => {
    const retrying = { ...req('queued'), error_code: 'provider_timeout' };
    const fetchRequest = vi.fn().mockResolvedValue(retrying);
    const result = await pollCollectionRequest(fetchRequest, { signal: new AbortController().signal, intervalMs: 1, timeoutMs: 20 });
    expect(result).toMatchObject({ status: 'failed', error_code: 'provider_timeout' });
    const bare = await pollCollectionRequest(vi.fn().mockResolvedValue(req('running')), { signal: new AbortController().signal, intervalMs: 1, timeoutMs: 5 });
    expect(bare?.error_code).toBe('test_fetch_timeout');
  });

  it('stops when aborted and returns null', async () => {
    const controller = new AbortController();
    const fetchRequest = vi.fn().mockImplementation(async () => { controller.abort(); return req('queued'); });
    expect(await pollCollectionRequest(fetchRequest, { signal: controller.signal, intervalMs: 1 })).toBeNull();
    expect(fetchRequest).toHaveBeenCalledTimes(1);
  });

  it('treats failed as terminal', async () => {
    const fetchRequest = vi.fn().mockResolvedValue(req('failed'));
    expect((await pollCollectionRequest(fetchRequest, { signal: new AbortController().signal, intervalMs: 1 }))?.status).toBe('failed');
  });
});
