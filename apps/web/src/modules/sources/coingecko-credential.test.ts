import { describe, expect, it, vi } from 'vitest';

const apiRequest = vi.fn().mockResolvedValue({ state: 'active', error_code: null });
vi.mock('@/core/api', async (orig) => ({ ...(await orig<object>()), apiRequest: (...args: unknown[]) => apiRequest(...args) }));

import { saveNativeRestCredential } from './api';

describe('saveNativeRestCredential', () => {
  it('PUTs the key with the connector revision to the native-rest slot', async () => {
    await saveNativeRestCredential('s1', 7, 'demo-key', 'csrf');
    const [path, init] = apiRequest.mock.calls[0];
    expect(path).toBe('/api/v1/connectors/s1/credentials/native-rest');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body)).toEqual({ expected_revision: 7, secret: 'demo-key' });
  });
});
