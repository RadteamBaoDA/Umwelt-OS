import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { ConnectorCatalogEntry, Source } from './api';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

import { ProviderGuide } from './provider-scope';

const entry = (over: Partial<ConnectorCatalogEntry>): ConnectorCatalogEntry => ({
  provider_id: 'alternative_me', label: 'Alternative.me Fear and Greed', auth_methods: ['none'], scope_fields: [], configuration_fields: [],
  quota_limits: {}, history_description: null, collection_modes: [], supports_history: false, supports_edit: false, supports_delete: false,
  availability: 'implemented', unavailable_operations: [], terms_url: 'https://alternative.me/crypto/fear-and-greed-index/',
  attribution: 'Display Alternative.me with a link adjacent to the index.', hosts: ['api.alternative.me'], default_interval_minutes: 1440,
  key_fields: [], runtime_verified: false, quota_policies: [{ policy_key: 'k', budget_kind: 'provider', window: 'day', unit: 'http_calls', limit_units: 8, basis: 'pilot_local' }], ...over,
});
const source = { last_success_at: '2026-10-01T00:00:00Z', collection_error_code: 'provider_unavailable' } as Source;

const view = (e: ConnectorCatalogEntry, s?: Source) => render(
  <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}><ProviderGuide entry={e} source={s} now={new Date('2026-10-09T00:00:00Z')} /></NextIntlClientProvider>);

describe('ProviderGuide', () => {
  it('shows attribution with terms link, catalog quota and cadence only', () => {
    view(entry({}));
    expect(screen.getByText(/Display Alternative.me/)).toBeTruthy();
    expect(screen.getByRole('link').getAttribute('href')).toBe('https://alternative.me/crypto/fear-and-greed-index/');
    expect(screen.getByText('8 http_calls per day')).toBeTruthy();
    expect(screen.getByText('Not required.')).toBeTruthy();
    expect(screen.getByText(/Not yet verified/)).toBeTruthy();
  });
  it('explains key prerequisite as masked and shows reference date / annual period', () => {
    view(entry({ provider_id: 'world_bank', key_fields: ['api_key'] }));
    expect(screen.getByText(/masked afterwards/)).toBeTruthy();
    expect(screen.getByText(/year it covers/)).toBeTruthy();
  });
  it('flags stale data and keeps last success next to the error', () => {
    view(entry({}), source);
    expect(screen.getByRole('status').textContent).toMatch(/Stale/);
    expect(screen.getByRole('alert').textContent).toMatch(/provider_unavailable · showing the last good data/);
  });
  it('renders nothing for entries without free-provider facts', () => {
    const { container } = view(entry({ terms_url: null, attribution: null }));
    expect(container.textContent).toBe('');
  });
});
