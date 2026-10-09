import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { metadata } from './page';
import { clearInviteToken, InviteAccept, takeInviteToken } from './invite-accept';

const replace = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ replace }) }));

const TOKEN = 'T'.repeat(43);

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
    <QueryClientProvider client={client}><InviteAccept /></QueryClientProvider>
  </NextIntlClientProvider>);
}

describe('invite page', () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    clearInviteToken();
    replace.mockReset();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
    window.history.replaceState(null, '', `/invitations/accept?token=${TOKEN}`);
  });
  afterEach(() => vi.unstubAllGlobals());

  it('opts out of Referer leakage', () => {
    expect(metadata.referrer).toBe('no-referrer');
  });

  it('strips the token from the URL and never renders it', async () => {
    fetchMock.mockImplementation((path: string) => path.includes('auth/session') ? json({ detail: 'x' }, 401) : json({ configured: false }));
    const { container } = renderPage();
    await screen.findByLabelText('Choose a password');
    expect(window.location.search).toBe('');
    expect(window.location.href).not.toContain(TOKEN);
    expect(container.innerHTML).not.toContain(TOKEN);
    expect(document.documentElement.innerHTML).not.toContain(TOKEN);
  });

  it('reads the token only once', () => {
    expect(takeInviteToken().present).toBe(true);
    expect(window.location.search).toBe('');
    expect(takeInviteToken().present).toBe(true); // held in memory, URL no longer needed
    clearInviteToken();
    expect(takeInviteToken().present).toBe(false);
  });

  it('requires a real password and does not log in after accepting', async () => {
    fetchMock.mockImplementation((path: string, init?: RequestInit) => {
      if (path.includes('auth/session')) return json({ detail: 'x' }, 401);
      if (path.includes('google/status')) return json({ configured: false });
      if (path.includes('auth/csrf')) return json({ csrfToken: 'c' });
      if (path.includes('invitations/accept')) {
        expect(JSON.parse(String(init?.body))).toMatchObject({ token: TOKEN, password: 'correct horse battery', google_enrollment: false });
        return json({ membership: { email: 'a@b.co', role: 'member', user_id: 2, membership_revision: 1 }, default_workspace: null });
      }
      return json({}, 404);
    });
    const user = userEvent.setup();
    renderPage();
    await user.type(await screen.findByLabelText('Choose a password'), 'short');
    await user.type(screen.getByLabelText('Confirm password'), 'short');
    await user.click(screen.getByRole('button', { name: 'Accept invitation' }));
    expect(await screen.findByText('Use at least 12 characters.')).toBeTruthy();
    expect(fetchMock.mock.calls.some((call: unknown[]) => String(call[0]).includes('invitations/accept'))).toBe(false);

    await user.clear(screen.getByLabelText('Choose a password'));
    await user.clear(screen.getByLabelText('Confirm password'));
    await user.type(screen.getByLabelText('Choose a password'), 'correct horse battery');
    await user.type(screen.getByLabelText('Confirm password'), 'correct horse battery');
    await user.click(screen.getByRole('button', { name: 'Accept invitation' }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/login?identifier=a%40b.co'));
    expect(fetchMock.mock.calls.some((call: unknown[]) => String(call[0]).includes('auth/login'))).toBe(false);
    expect(takeInviteToken().present).toBe(false);
  });

  it('shows a neutral message for a signed-in wrong account', async () => {
    fetchMock.mockImplementation((path: string) => {
      if (path.includes('auth/session')) return json({ authenticated: true, csrfToken: 'c' });
      return json({ detail: 'Invitation belongs to another account' }, 403);
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Accept invitation' }));
    expect(await screen.findByText(/cannot be accepted with the current account/)).toBeTruthy();
    expect(document.body.textContent).not.toContain('belongs to another account');
  });
});
