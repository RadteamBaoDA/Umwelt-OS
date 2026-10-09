import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { ConnectorCatalogEntry, ProviderTerms as Terms, Source } from './api';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
const api = vi.hoisted(() => ({ getProviderTerms: vi.fn(), acknowledgeProviderTerms: vi.fn(), getSourceIngestion: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

import { ProviderGuide, ProviderTerms } from './provider-scope';
import { SyncHistory } from './sync-history';

const entry = {
  provider_id: 'frankfurter', label: 'Frankfurter', auth_methods: ['none'], scope_fields: [], configuration_fields: [], quota_limits: {}, history_description: null,
  collection_modes: [], supports_history: false, supports_edit: false, supports_delete: false, availability: 'implemented', unavailable_operations: [],
  terms_url: 'https://frankfurter.dev', terms_checked_on: '2026-10-07', attribution: 'Credit Frankfurter.', key_fields: [],
  example_config: { timezone: 'Asia/Ho_Chi_Minh', schedule_interval_minutes: 1440 }, sample_output: { base: 'EUR', rate: 1.0 },
} as ConnectorCatalogEntry;
const wrap = (ui: React.ReactNode) => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>{ui}</NextIntlClientProvider>
  </QueryClientProvider>);
const terms = (over: Partial<Terms>): Terms => ({ source_id: 's', provider_id: 'frankfurter', terms_revision: 1, terms_url: 'u', terms_version: '2026-10-07', checked_on: '2026-10-07', declared_use: 'personal', operator_review_state: 'pending', reviewed_allowed_use: null, eligible: false, decision: 'terms_not_acknowledged', ...over });

beforeEach(() => { api.getProviderTerms.mockReset(); api.acknowledgeProviderTerms.mockReset(); });

describe('ProviderGuide examples', () => {
  it('shows labelled static example config and sample output, and tests via the validate callback', () => {
    const onTest = vi.fn();
    wrap(<ProviderGuide entry={entry} onTest={onTest} />);
    expect(screen.getByTestId('guide-example-config').textContent).toContain('"schedule_interval_minutes": 1440');
    expect(screen.getByTestId('guide-sample-output').textContent).toContain('"base": "EUR"');
    expect(screen.getByText(/Example only/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect(onTest).toHaveBeenCalledTimes(1);
  });
  it('disables the test button until the source exists', () => {
    wrap(<ProviderGuide entry={entry} />);
    expect((screen.getByRole('button', { name: 'Test connection' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('ProviderTerms', () => {
  it('states that terms are required and acknowledges the current catalog version', async () => {
    api.getProviderTerms.mockResolvedValue(null);
    api.acknowledgeProviderTerms.mockResolvedValue(terms({ eligible: true, decision: 'ok' }));
    wrap(<ProviderTerms sourceId="s" entry={entry} csrfToken="c" />);
    expect(screen.getByText(/required before this source can be activated/)).toBeTruthy();
    const save = screen.getByRole('button', { name: 'Acknowledge terms' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(screen.getByRole('combobox'));
    fireEvent.click(await screen.findByRole('option', { name: 'Personal' }));
    fireEvent.click(screen.getByRole('checkbox'));
    await waitFor(() => expect(save.disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(api.acknowledgeProviderTerms).toHaveBeenCalledWith('s', 'personal', '2026-10-07', 'c'));
    expect(await screen.findByText('Terms accepted. The source can be activated.')).toBeTruthy();
  });
});

describe('SyncHistory schedule', () => {
  it('renders next_due_at and retry_at when present', async () => {
    api.getSourceIngestion.mockResolvedValue({ current_run: null, items: [], next_cursor: null });
    const source = { id: 's', last_success_at: null, next_due_at: '2026-10-10T08:00:00Z', retry_at: '2026-10-09T09:00:00Z', status: 'active', provider: 'frankfurter' } as Source;
    wrap(<SyncHistory source={source} />);
    expect(await screen.findByText(/Next collection due/)).toBeTruthy();
    expect(screen.getByText(/Retry after/)).toBeTruthy();
  });
});

describe('messages', () => {
  it('keeps en and vi keys in parity', () => {
    const en = Object.keys(sourcesMessages['en-us']).sort();
    const vi_ = Object.keys((sourcesMessages as Record<string, Record<string, string>>)['vi-vi'] ?? {}).sort();
    expect(vi_).toEqual(en);
  });
});
