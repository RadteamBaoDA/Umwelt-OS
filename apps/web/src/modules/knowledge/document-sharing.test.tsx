import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { sharingMessages } from '@/core/messages/sharing';

const api = vi.hoisted(() => ({
  listWorkspaceMembers: vi.fn(), listShares: vi.fn(), grantShare: vi.fn(), revokeShare: vi.fn(),
}));
const ws = vi.hoisted(() => ({ isOwner: true }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ selection: { id: 'w1', role: ws.isOwner ? 'owner' : 'member', revision: 1 }, isOwner: ws.isOwner }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));

import { DocumentShareButton } from './document-sharing';

const member = { user_id: 7, email: 'm@example.com', role: 'member' as const, membership_revision: 3 };
function renderButton() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ sharing: sharingMessages['en-us'] }}><DocumentShareButton documentId="d1" currentVersion={4} /></NextIntlClientProvider></QueryClientProvider>);
}

describe('DocumentShareButton', () => {
  beforeEach(() => {
    vi.clearAllMocks(); ws.isOwner = true;
    api.listWorkspaceMembers.mockResolvedValue({ items: [{ ...member, user_id: 1, role: 'owner', email: 'o@example.com' }, member], next_cursor: null });
    api.listShares.mockResolvedValue({ items: [], next_cursor: null });
  });

  it('renders nothing for members', () => {
    ws.isOwner = false;
    const { container } = renderButton();
    expect(container).toBeEmptyDOMElement();
    expect(api.listWorkspaceMembers).not.toHaveBeenCalled();
  });

  it('shares one chosen member, binding the current version, with no workspace-wide option', async () => {
    api.grantShare.mockResolvedValue({});
    renderButton();
    await userEvent.click(screen.getByRole('button', { name: 'Share' }));
    expect(screen.queryByText('o@example.com')).toBeNull();
    await userEvent.click(await screen.findByRole('button', { name: 'Share with m@example.com' }));
    await waitFor(() => expect(api.grantShare).toHaveBeenCalledWith('w1', 'document', 'd1', member, 4, 'csrf'));
    expect(screen.queryByText(/entire workspace/i)).toBeNull();
  });

  it('revokes with the membership revision', async () => {
    api.listShares.mockResolvedValue({ items: [{ member_user_id: 7, revoked_at: null }], next_cursor: null });
    api.revokeShare.mockResolvedValue(undefined);
    renderButton();
    await userEvent.click(screen.getByRole('button', { name: 'Share' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Revoke access for m@example.com' }));
    await waitFor(() => expect(api.revokeShare).toHaveBeenCalledWith('w1', 'document', 'd1', member, 'csrf'));
  });

  it('shows the refresh message on 409', async () => {
    api.grantShare.mockRejectedValue(new ApiError(409, 'conflict'));
    renderButton();
    await userEvent.click(screen.getByRole('button', { name: 'Share' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Share with m@example.com' }));
    expect(await screen.findByText(sharingMessages['en-us'].errorRefresh)).toBeTruthy();
  });
});
