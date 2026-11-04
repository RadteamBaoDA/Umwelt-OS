import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';

const request = vi.hoisted(() => vi.fn());
vi.mock('@/core/api', async (original) => ({ ...(await original<typeof import('@/core/api')>()), apiRequest: request }));

import { allowedIntervals, clampInterval, collectionTooSoonRetryAt, createScopedSource, intervalBelowMinimum, minIntervalMinutes, renameSource } from './api';

const spec = { provider: 'arxiv', type: 'api' as const, name: 'arXiv', configuration: { timeout_seconds: 30, timezone: 'UTC', schedule_interval_minutes: 360 as const } };
beforeEach(() => { request.mockReset(); });

describe('createScopedSource', () => {
  it('creates, saves, acknowledges terms when given, and activates at the returned revision', async () => {
    request.mockImplementation(async (path: string) => path.endsWith('/configuration') ? { desired_revision: 3 } : { id: 's1' });
    await createScopedSource({ ...spec, termsVersion: '2026-11-04' }, 'c');
    const paths = request.mock.calls.map(([path, options]) => `${options?.method ?? 'GET'} ${path}`);
    expect(paths).toEqual(['POST /api/v1/sources', 'PUT /api/v1/connectors/s1/configuration', 'PUT /api/v1/connectors/sources/s1/terms', 'POST /api/v1/connectors/s1/activate']);
    expect(JSON.parse(request.mock.calls[3][1].body)).toMatchObject({ expected_revision: 3 });
  });

  it('archives the half-made source when activation fails and rethrows', async () => {
    request.mockImplementation(async (path: string, options?: { method?: string }) => {
      if (path.endsWith('/activate')) throw new ApiError(500, 'boom');
      if (options?.method === 'DELETE') return undefined;
      return path.endsWith('/configuration') ? { desired_revision: 1 } : { id: 's1' };
    });
    await expect(createScopedSource(spec, 'c')).rejects.toThrow('boom');
    expect(request).toHaveBeenLastCalledWith('/api/v1/sources/s1', expect.objectContaining({ method: 'DELETE' }));
  });
});

describe('rename and interval helpers', () => {
  it('renameSource patches only the name', async () => {
    request.mockResolvedValue({});
    await renameSource('s1', 'New', 'c');
    expect(request).toHaveBeenCalledWith('/api/v1/sources/s1', expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ name: 'New' }) }));
  });

  it('applies the provider floor, treating a missing one as 15', () => {
    expect(minIntervalMinutes({})).toBe(15);
    expect(allowedIntervals([15, 30, 60, 360, 1440] as const, { min_interval_minutes: 360 })).toEqual([360, 1440]);
    expect(clampInterval(30, { min_interval_minutes: 360 })).toBe(360);
    expect(clampInterval(60, undefined)).toBe(60);
  });

  it('parses collection_too_soon retry_after (ISO or seconds) and the 422 minimum', () => {
    const now = new Date('2026-11-04T00:00:00Z');
    expect(collectionTooSoonRetryAt(new ApiError(429, 'collection_too_soon', { code: 'collection_too_soon', retry_after: '2026-11-04T06:00:00Z' }), now)?.toISOString()).toBe('2026-11-04T06:00:00.000Z');
    expect(collectionTooSoonRetryAt(new ApiError(429, 'collection_too_soon', { retry_after: 3600 }), now)?.toISOString()).toBe('2026-11-04T01:00:00.000Z');
    expect(collectionTooSoonRetryAt(new ApiError(429, 'collection_too_soon'), now)).toBeNull();
    expect(collectionTooSoonRetryAt(new ApiError(500, 'x'), now)).toBeUndefined();
    expect(intervalBelowMinimum(new ApiError(422, 'schedule_interval_below_minimum', { minimum: 360 }))).toEqual({ minimum: 360 });
    expect(intervalBelowMinimum(new ApiError(422, 'other'))).toBeUndefined();
  });
});
