import { describe, expect, it, vi } from 'vitest';

const request = vi.hoisted(() => vi.fn());
vi.mock('@/core/api', () => ({ apiRequest: request, csrfHeaders: () => ({}) }));
import { listAllSources } from './api';

describe('listAllSources', () => {
  it('follows the cursor across pages and returns one flat page', async () => {
    request
      .mockResolvedValueOnce({ items: [{ id: 'a' }], next_cursor: 'c1' })
      .mockResolvedValueOnce({ items: [{ id: 'b' }], next_cursor: null });
    const page = await listAllSources();
    expect(page.items.map((s) => s.id)).toEqual(['a', 'b']);
    expect(request.mock.calls[1][0]).toContain('cursor=c1');
    expect(request.mock.calls[0][0]).toContain('limit=100');
  });
});
