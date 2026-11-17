import { beforeEach, describe, expect, it, vi } from 'vitest';
import { listWorldObservationWindow } from './api';

const request = vi.hoisted(() => vi.fn());
vi.mock('@/core/api', async (original) => ({ ...(await original<typeof import('@/core/api')>()), apiRequest: request }));

const window = { sourceIds: ['s1'], from: new Date('2026-10-01'), to: new Date('2026-10-02') };

describe('listWorldObservationWindow', () => {
  beforeEach(() => {
    request.mockReset();
    request.mockImplementation(async () => ({ items: [], next_cursor: 'more', truncated: false }));
  });

  it('walks at most five pages by default and reports truncation', async () => {
    const result = await listWorldObservationWindow(window);
    expect(request).toHaveBeenCalledTimes(5);
    expect(result.truncated).toBe(true);
  });

  it('honours a smaller maxPages cap so latest-value readers fetch a single page', async () => {
    const result = await listWorldObservationWindow({ ...window, maxPages: 1 });
    expect(request).toHaveBeenCalledTimes(1);
    expect(result.truncated).toBe(true);
  });

  it('sends history=true only when history mode is requested', async () => {
    request.mockImplementation(async () => ({ items: [], next_cursor: null, truncated: false }));
    await listWorldObservationWindow({ ...window, history: true });
    expect(request.mock.calls[0][0]).toContain('history=true');
    request.mockClear();
    await listWorldObservationWindow(window);
    expect(request.mock.calls[0][0]).not.toContain('history');
  });
});
