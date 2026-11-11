import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { globalSettingsMessages } from '@/core/messages/global-settings';
import { ProviderReviewTable } from './provider-review';

const api = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('@/core/api', () => ({
  retrySessionConflict: () => false,
  apiRequest: api.request,
  csrfHeaders: (t: string) => ({ 'X-CSRF-Token': t }),
}));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/modules/sources/api', () => ({
  connectorKeys: { catalog: ['catalog'] },
  getConnectorCatalog: async () => [{ provider_id: 'acme', label: 'Acme Jobs' }],
}));

const row = {
  workspace_id: 'w1',
  workspace_name: 'Home',
  source_id: 's1',
  provider_id: 'acme',
  terms_url: 'https://acme.test/terms',
  terms_version: '2026-01-02',
  declared_use: 'personal',
  operator_review_state: 'pending',
};

describe('ProviderReviewTable', () => {
  beforeEach(() => api.request.mockReset().mockResolvedValue({}));
  it('lists the row, keeps Approve disabled without evidence, and submits the unchanged payload', async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <NextIntlClientProvider locale="en-US" messages={{ globalSettings: globalSettingsMessages['en-us'] }} onError={() => undefined}>
          <ProviderReviewTable rows={[row]} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    await screen.findByText('Acme Jobs');
    fireEvent.click(screen.getByRole('button', { name: 'Review' }));
    const approve = await screen.findByRole('button', { name: 'Approve' });
    expect((approve as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/Add an evidence reference/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Evidence reference'), { target: { value: ' https://proof ' } });
    fireEvent.click(approve);
    await waitFor(() => expect(api.request).toHaveBeenCalled());
    const [url, init] = api.request.mock.calls[0];
    expect(url).toBe('/api/v1/operator/connectors/w1/s1/terms-review');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body)).toEqual({
      state: 'approved',
      allowed_use: 'personal',
      evidence_ref: 'https://proof',
      reviewed_terms_version: '2026-01-02',
    });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull());
  });
  it('submits the exact live-map acknowledgement without changing connector reviews', async () => {
    const live = {
      workspace_id: 'w2',
      workspace_name: 'Other',
      provider_id: 'eonet',
      terms_url: 'https://example.org/terms',
      terms_version: '2026-01-02',
      declared_use: 'personal',
      acknowledged_at: '2026-11-04T00:00:00+00:00',
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <NextIntlClientProvider locale="en-US" messages={{ globalSettings: globalSettingsMessages['en-us'] }} onError={() => undefined}>
          <ProviderReviewTable rows={[live]} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Review' }));
    fireEvent.change(await screen.findByLabelText('Evidence reference'), { target: { value: 'proof' } });
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(api.request).toHaveBeenCalled());
    const [url, init] = api.request.mock.calls[0];
    expect(url).toBe('/api/v1/operator/live-map/w2/eonet/terms-review');
    expect(JSON.parse(init.body)).toEqual({
      state: 'approved',
      evidence_ref: 'proof',
      version: '2026-01-02',
      declared_use: 'personal',
      acknowledged_at: live.acknowledged_at,
    });
  });
});
