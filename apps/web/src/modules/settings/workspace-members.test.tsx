import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { invitationStatus, WorkspaceMembers, type InvitationRead } from './workspace-members';

const ws = { selection: { id: 'w1', role: 'owner' as 'owner' | 'member', revision: 4 }, isOwner: true };
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ws }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));

const future = new Date(Date.now() + 86_400_000).toISOString();
const invitation: InvitationRead = {
  id: 'i1', email: 'p@x.co', created_at: future, expires_at: future, accepted_at: null, accepted_by_user_id: null, revoked_at: null,
};

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
}

function renderMembers() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
    <QueryClientProvider client={client}><WorkspaceMembers /></QueryClientProvider>
  </NextIntlClientProvider>);
}

describe('workspace members', () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    ws.selection.role = 'owner'; ws.isOwner = true;
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });

  it('derives invitation status', () => {
    expect(invitationStatus(invitation)).toBe('pending');
    expect(invitationStatus({ ...invitation, accepted_at: future })).toBe('accepted');
    expect(invitationStatus({ ...invitation, revoked_at: future })).toBe('revoked');
    expect(invitationStatus({ ...invitation, expires_at: '2000-01-01T00:00:00Z' })).toBe('expired');
  });

  it('hides management from members and sends no requests', () => {
    ws.selection.role = 'member'; ws.isOwner = false;
    renderMembers();
    expect(screen.getByText('Only the workspace owner can manage members.')).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('creates an invitation with expected_revision and shows the one-time link', async () => {
    fetchMock.mockImplementation((path: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        expect(JSON.parse(String(init.body))).toEqual({ email: 'new@x.co', expected_revision: 4 });
        return json({ invitation_id: 'i2', invitation_url: 'https://app/invitations/accept?token=abc', expires_at: future }, 201);
      }
      return json({ items: path.includes('/members') ? [{ user_id: 1, email: 'o@x.co', role: 'owner', membership_revision: 1 }] : [invitation], next_cursor: null });
    });
    const user = userEvent.setup();
    renderMembers();
    await screen.findByText('p@x.co');
    await user.type(screen.getByLabelText('Email address'), 'new@x.co');
    await user.click(screen.getByRole('button', { name: 'Create invitation' }));
    expect(await screen.findByDisplayValue('https://app/invitations/accept?token=abc')).toBeTruthy();
  });

  it('revokes with If-Match and removes members with If-Match', async () => {
    const deletes: Array<[string, string | null]> = [];
    fetchMock.mockImplementation((path: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') {
        deletes.push([path, new Headers(init.headers).get('If-Match')]);
        return json(null, 204);
      }
      return json({ items: path.includes('/members') ? [{ user_id: 7, email: 'm@x.co', role: 'member', membership_revision: 2 }] : [invitation], next_cursor: null });
    });
    const user = userEvent.setup();
    renderMembers();
    await screen.findByText('p@x.co');
    await user.click(screen.getByRole('button', { name: 'Revoke' }));
    await user.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(deletes).toContainEqual(['/api/v1/workspaces/w1/invitations/i1', '"4"']));
    await user.click(await screen.findByRole('button', { name: 'Remove' }));
    await user.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(deletes).toContainEqual(['/api/v1/workspaces/w1/members/7', '"4"']));
  });
});
