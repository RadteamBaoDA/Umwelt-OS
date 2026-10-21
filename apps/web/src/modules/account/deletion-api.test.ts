import { afterEach, describe, expect, it, vi } from 'vitest';
import { acceptAccountDeletion } from './deletion-api';

afterEach(() => vi.unstubAllGlobals());

describe('account deletion acceptance', () => {
  it('recovers a committed deletion through the preissued receipt after a lost POST response', async () => {
    const state = { status: 'running', phase: 'external', error_code: null };
    const fetcher = vi.fn().mockRejectedValueOnce(new TypeError('connection lost'))
      .mockResolvedValueOnce(Response.json(state));
    vi.stubGlobal('fetch', fetcher);
    await expect(acceptAccountDeletion('password', 'csrf')).resolves.toEqual(state);
    expect(fetcher.mock.calls[1][0]).toBe('/api/v1/auth/account/deletion-status');
    expect(fetcher.mock.calls[0][1].body).toBe(JSON.stringify({ current_password: 'password', confirmation: 'DELETE' }));
  });

  it('keeps a wrong password error and never treats a failed request as accepted', async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ detail: { code: 'password_incorrect' } }, { status: 400 }));
    vi.stubGlobal('fetch', fetcher);
    await expect(acceptAccountDeletion('wrong', 'csrf')).rejects.toMatchObject({ code: 'password_incorrect', status: 400 });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('recovers acceptance when headers arrive but the response body is lost', async () => {
    const state = { status: 'queued', phase: 'queued', error_code: null };
    const fetcher = vi.fn().mockResolvedValueOnce({ ok: true, status: 202, json: () => Promise.reject(new TypeError('body lost')) })
      .mockResolvedValueOnce(Response.json(state));
    vi.stubGlobal('fetch', fetcher);
    await expect(acceptAccountDeletion('password', 'csrf')).resolves.toEqual(state);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('recovers an ambiguous 401 without broadcasting global session-expiry events', async () => {
    const unauthorized = vi.fn();
    window.addEventListener('bbd:unauthorized', unauthorized);
    const state = { status: 'queued', phase: 'external', error_code: null };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(Response.json({}, { status: 401 })).mockResolvedValueOnce(Response.json(state)));
    await expect(acceptAccountDeletion('password', 'csrf')).resolves.toEqual(state);
    expect(unauthorized).not.toHaveBeenCalled();
    window.removeEventListener('bbd:unauthorized', unauthorized);
  });
});
