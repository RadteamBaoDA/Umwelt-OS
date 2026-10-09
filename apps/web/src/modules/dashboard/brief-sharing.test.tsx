import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { sharingMessages } from '@/core/messages/sharing';

const api = vi.hoisted(() => ({ listWorkspaceMembers: vi.fn(), listShares: vi.fn(), grantShare: vi.fn(), getDocument: vi.fn() }));
vi.mock('@/modules/knowledge/api', async (original) => ({ ...(await original<typeof import('@/modules/knowledge/api')>()), ...api }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ selection: { id: 'w1', role: 'owner', revision: 1 }, isOwner: true }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('next/link', () => ({ default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a> }));

import { BriefShareButton } from './brief-sharing';

const member = { user_id: 7, email: 'm@example.com', role: 'member' as const, membership_revision: 3 };

describe('BriefShareButton', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listWorkspaceMembers.mockResolvedValue({ items: [member], next_cursor: null });
    api.listShares.mockResolvedValue({ items: [], next_cursor: null });
    api.getDocument.mockResolvedValue({ title: 'Evidence doc' });
  });

  it('links only the owner-visible documents listed by a 409 brief_evidence_not_shared', async () => {
    api.grantShare.mockRejectedValue(new ApiError(409, 'x', { code: 'brief_evidence_not_shared' }));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ json: async () => ({ detail: { code: 'brief_evidence_not_shared', document_ids: ['doc-1'] } }) }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ sharing: sharingMessages['en-us'] }}><BriefShareButton brief={{ id: 'b1', revision: 2 }} /></NextIntlClientProvider></QueryClientProvider>);
    await userEvent.click(screen.getByRole('button', { name: 'Share' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Share with m@example.com' }));
    const link = await screen.findByRole('link', { name: 'Evidence doc' });
    expect(link.getAttribute('href')).toBe('/knowledge/documents/doc-1');
    expect(screen.getAllByRole('link')).toHaveLength(1);
  });
});
