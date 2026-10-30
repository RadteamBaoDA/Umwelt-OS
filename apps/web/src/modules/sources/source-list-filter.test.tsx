import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { gadgetSettingsMessages } from '@/core/messages/gadget-settings';
import { sourcesMessages } from '@/core/messages/sources';

const nav = vi.hoisted(() => ({ params: new URLSearchParams(), replace: vi.fn() }));
vi.mock('next/navigation', () => ({ useSearchParams: () => nav.params, useRouter: () => ({ replace: nav.replace }), usePathname: () => '/settings/sources' }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 't' }) }));
vi.mock('@/core/guarded-navigation', () => ({ useGuardedNavigation: () => ({ ensureSourcesDocument: () => true }) }));
vi.mock('./connector-editor', () => ({ ConnectorEditor: () => <div /> }));
const builtin = (id: string, label: string, shapes: string[]) => ({
  id, label, category: 'news', eligibility: 'open', terms_url: 'https://x.org', terms_checked_on: '2026-10-07', attribution: 'a',
  execution: 'supported', source_id: null, enabled: false, data_shapes: shapes,
});
vi.mock('./api', async (original) => ({
  ...(await original<typeof import('./api')>()),
  listSources: vi.fn(async () => ({ items: [{ id: 'r1', type: 'rss', provider: 'rss', name: 'Existing row', status: 'active' }], next_cursor: null })),
  listBuiltinSources: vi.fn(async () => [builtin('noaa_kp', 'NOAA Kp index', ['measurement']), builtin('bbc_world', 'BBC World', ['news'])]),
  getConnectorActivation: vi.fn(async () => ({ state: 'active', error_code: null })),
  getConnectorCatalog: vi.fn(async () => []),
  getMonotonicConnectorConfiguration: vi.fn(async () => ({ configuration: { schedule_interval_minutes: 60 } })),
}));

import { SourceList } from './source-list';
import { parseShapeFilter } from './shape-filter';

/** Renders the Sources list with the URL query set to `query`. */
function wrap(query: string) {
  nav.params = new URLSearchParams(query);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'], gadgetSettings: gadgetSettingsMessages['en-us'] }}><SourceList /></NextIntlClientProvider>
  </QueryClientProvider>);
}

describe('Sources shape filter', () => {
  afterEach(() => { cleanup(); nav.replace.mockClear(); });

  it('parseShapeFilter accepts the series shape the chart template links with', () => {
    expect(parseShapeFilter(new URLSearchParams('shapes=series&for=finance_chart'))).toEqual({ shapes: ['series'], renderer: 'finance_chart' });
  });

  it('parseShapeFilter drops unknown shapes', () => {
    expect(parseShapeFilter(new URLSearchParams('shapes=measurement,bogus&for=finance_chart'))).toEqual({ shapes: ['measurement'], renderer: 'finance_chart' });
    expect(parseShapeFilter(new URLSearchParams('shapes=bogus')).shapes).toEqual([]);
  });

  it('shows banner for ?shapes=measurement&for=finance_chart and keeps existing rows', async () => {
    wrap('shapes=measurement&for=finance_chart');
    expect(await screen.findByText(/Showing datasources for: Chart/)).toBeTruthy();
    expect(await screen.findByText('Existing row')).toBeTruthy();
  });

  it('builtin wizard lists only measurement built-ins', async () => {
    wrap('shapes=measurement&for=finance_chart');
    await userEvent.click(await screen.findByRole('button', { name: 'Add built-in' }));
    expect(await screen.findByRole('button', { name: /NOAA Kp index/ })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /BBC World/ })).toBeNull();
  });

  it('ignores unknown shapes', async () => {
    wrap('shapes=bogus');
    await screen.findByText('Existing row');
    expect(screen.queryByText(/Showing datasources for/)).toBeNull();
  });

  it('clear filter removes params', async () => {
    wrap('shapes=measurement&for=finance_chart');
    await userEvent.click(await screen.findByRole('button', { name: 'Clear filter' }));
    expect(nav.replace).toHaveBeenCalledWith('/settings/sources');
  });
});
