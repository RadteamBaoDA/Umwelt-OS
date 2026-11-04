import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { commonMessages } from '@/core/messages/common';

vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ selection: { id: 'workspace' }, isOwner: true }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'token' }) }));
const request = vi.hoisted(() => vi.fn());
vi.mock('@/core/api', () => ({ apiRequest: request, csrfHeaders: () => ({}) }));
import { LiveMapConfig } from './live-map-config';

describe('live map consent', () => {
  it('lets a workspace owner submit review-class terms for operator review', async () => {
    const initial = { revision: 1, openweather_configured: false, firms_configured: false, sec_contact_email: null, is_operator: false, terms_catalog: [{ id: 'eonet', label: 'EONET', terms_url: 'https://example.com', version: '1', eligibility: 'review', accepted: false, acknowledged: false, review_state: null, attribution: 'NASA' }] };
    request.mockImplementation(async (_url, options) => options?.method === 'PUT' ? { ...initial, revision: 2, terms_catalog: [{ ...initial.terms_catalog[0], acknowledged: true, review_state: 'pending' }] } : initial);
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], common: commonMessages['en-us'] }}><LiveMapConfig /></NextIntlClientProvider></QueryClientProvider>);
    await userEvent.click(await screen.findByRole('checkbox', { name: 'EONET' }));
    await userEvent.click(screen.getByRole('button', { name: 'Accept selected terms' }));
    expect(await screen.findByText(/Pending operator review/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Withdraw EONET consent' })).toBeTruthy();
  });

  it('withdraws one accepted provider at its current revision and permits review again', async () => {
    const initial = { revision: 3, openweather_configured: false, firms_configured: false, sec_contact_email: null, is_operator: true, terms_catalog: [{ id: 'usgs', label: 'USGS', terms_url: 'https://example.com', version: '1', eligibility: 'open', accepted: true, acknowledged: true, review_state: 'pending', attribution: 'USGS' }] };
    request.mockImplementation(async (_url, options) => options?.method === 'PUT' ? { ...initial, revision: 4, terms_catalog: [{ ...initial.terms_catalog[0], accepted: false, acknowledged: false }] } : initial);
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], common: commonMessages['en-us'] }}><LiveMapConfig /></NextIntlClientProvider></QueryClientProvider>);
    await userEvent.click(await screen.findByRole('button', { name: 'Withdraw USGS consent' }));
    await waitFor(() => expect(request.mock.calls.some(([, options]) => options?.method === 'PUT' && JSON.parse(options.body).terms_revoke?.[0] === 'usgs' && JSON.parse(options.body).expected_revision === 3)).toBe(true));
    expect((await screen.findByRole('checkbox', { name: 'USGS' })).getAttribute('aria-checked')).toBe('false');
  });
});
