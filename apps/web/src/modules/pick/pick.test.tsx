import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { needsLoginRedirect } from '@/core/session-gate';
import { buildPickUrl, PickQrButton } from './pick-qr-modal';
import { PickPhone } from './pick-phone';

const SID = 'A'.repeat(22);

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
}

function wrap(node: React.ReactNode, locale: 'en-us' | 'vi-vi' = 'en-us') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<NextIntlClientProvider locale={locale === 'en-us' ? 'en-US' : 'vi-VN'} messages={messages[locale]}>
    <QueryClientProvider client={client}>{node}</QueryClientProvider>
  </NextIntlClientProvider>);
}

afterEach(() => { vi.restoreAllMocks(); window.history.replaceState(null, '', '/'); });

describe('pick catalogs and routing', () => {
  it('has identical en/vi keys and a public /pick route', () => {
    expect(Object.keys(messages['vi-vi'].pick).sort()).toEqual(Object.keys(messages['en-us'].pick).sort());
    expect(needsLoginRedirect('/pick', false)).toBe(false);
  });

  it('swaps a loopback host for the LAN address only', () => {
    expect(buildPickUrl('http://localhost:3000', SID, '192.168.1.5')).toBe(`http://192.168.1.5:3000/pick?session=${SID}`);
    expect(buildPickUrl('https://os.example.com', SID, '192.168.1.5')).toBe(`https://os.example.com/pick?session=${SID}`);
  });
});

describe('PickQrButton', () => {
  beforeEach(() => vi.useRealTimers());

  it('renders nothing while the feature is off', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => json({ detail: 'Not found' }, 404));
    wrap(<PickQrButton csrfToken="t" onPrompt={vi.fn()} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('shows waiting, then scanned, then delivers the picked prompt once and closes', async () => {
    const polls = [{ state: 'waiting', prompt: null }, { state: 'scanned', prompt: null }, { state: 'picked', prompt: 'DNA Helix' }];
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith('/status')) return json({ enabled: true });
      if (init?.method === 'POST') return json({ session_id: SID, expires_in: 600, lan_ip: '192.168.1.5' });
      return json(polls.shift() ?? { state: 'picked', prompt: null });
    });
    const onPrompt = vi.fn();
    wrap(<PickQrButton csrfToken="t" onPrompt={onPrompt} />);
    await userEvent.click(await screen.findByRole('button', { name: messages['en-us'].pick.open }));
    expect(await screen.findByTestId('pick-qr')).toBeTruthy();
    expect(await screen.findByText(messages['en-us'].pick.waiting)).toBeTruthy();
    expect(await screen.findByText(messages['en-us'].pick.scanned, {}, { timeout: 4000 })).toBeTruthy();
    await waitFor(() => expect(onPrompt).toHaveBeenCalledWith('DNA Helix'), { timeout: 4000 });
    expect(onPrompt).toHaveBeenCalledTimes(1);
  }, 12000);

  it('shows the expired state when the session is gone', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith('/status')) return json({ enabled: true });
      if (init?.method === 'POST') return json({ session_id: SID, expires_in: 600, lan_ip: null });
      return json({ detail: 'Not found' }, 404);
    });
    wrap(<PickQrButton csrfToken="t" onPrompt={vi.fn()} />, 'vi-vi');
    await userEvent.click(await screen.findByRole('button', { name: messages['vi-vi'].pick.open }));
    expect(await screen.findByText(messages['vi-vi'].pick.expired)).toBeTruthy();
  });
});

describe('PickPhone', () => {
  it('shows offered options and submits only the chosen index', async () => {
    window.history.replaceState(null, '', `/pick?session=${SID}`);
    const calls: Array<[string, RequestInit | undefined]> = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      calls.push([String(input), init]);
      if (init?.method === 'PUT') return json({ options: [{ index: 1, label: 'DNA Helix' }, { index: 4, label: 'Ocean Waves' }, { index: 9, label: 'Atom Model' }] });
      return Promise.resolve(new Response(null, { status: 204 }));
    });
    wrap(<PickPhone />);
    await userEvent.click(await screen.findByRole('button', { name: 'Ocean Waves' }));
    expect(await screen.findByText(messages['en-us'].pick.phoneDone)).toBeTruthy();
    const post = calls.find(([, init]) => init?.method === 'POST')!;
    expect(post[0]).toBe(`/api/v1/pick/phone/${SID}`);
    expect(JSON.parse(String(post[1]?.body))).toEqual({ index: 4 });
  });

  it('shows invalid for a missing id or an unknown session without calling the API when no id', async () => {
    const spy = vi.spyOn(globalThis, 'fetch');
    wrap(<PickPhone />);
    expect(await screen.findByText(messages['en-us'].pick.phoneInvalid)).toBeTruthy();
    expect(spy).not.toHaveBeenCalled();
  });

  it('shows invalid on 404 and the rate-limit message on 429', async () => {
    window.history.replaceState(null, '', `/pick?session=${SID}`);
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(() => json({ detail: 'x' }, 404));
    const { unmount } = wrap(<PickPhone />);
    expect(await screen.findByText(messages['en-us'].pick.phoneInvalid)).toBeTruthy();
    unmount();
    spy.mockImplementation(() => json({ detail: 'x' }, 429));
    wrap(<PickPhone />);
    expect(await screen.findByText(messages['en-us'].pick.phoneTooMany)).toBeTruthy();
  });

  it('scans once under StrictMode double effects so the burned scan is not seen as invalid', async () => {
    window.history.replaceState(null, '', `/pick?session=${SID}`);
    let scans = 0;
    vi.spyOn(globalThis, 'fetch').mockImplementation((_input, init) => {
      if (init?.method !== 'PUT') return Promise.resolve(new Response(null, { status: 204 }));
      scans += 1;
      // The server burns the session on the first scan (GETDEL); any repeat is a 404.
      return scans === 1 ? json({ options: [{ index: 1, label: 'DNA Helix' }] }) : json({ detail: 'x' }, 404);
    });
    wrap(<StrictMode><PickPhone /></StrictMode>);
    expect(await screen.findByRole('button', { name: 'DNA Helix' })).toBeTruthy();
    expect(scans).toBe(1);
    expect(screen.queryByText(messages['en-us'].pick.phoneInvalid)).toBeNull();
  });
});
