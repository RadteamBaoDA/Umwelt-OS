import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 't' }) }));
vi.mock('@/core/guarded-navigation', () => ({ useGuardedNavigation: () => ({ ensureSourcesDocument: () => true }) }));
vi.mock('./connector-editor', () => ({ ConnectorEditor: () => <div /> }));
vi.mock('./api', async (original) => ({
  ...(await original<typeof import('./api')>()),
  listSources: vi.fn(async () => ({ items: [{ id: 'm1', type: 'mcp', provider: 'mcp', name: 'Mcp one', status: 'active' }], next_cursor: null })),
  listBuiltinSources: vi.fn(async () => []),
  getConnectorActivation: vi.fn(async () => ({ state: 'active', error_code: null })),
  getConnectorCatalog: vi.fn(async () => []),
  getConnectorConfiguration: vi.fn(async () => ({ configuration: { schedule_interval_minutes: 60 } })),
}));

import { SourceList } from './source-list';

describe('SourceList MCP rows', () => {
  afterEach(cleanup);

  it('offers Configure so grants reviewed after the wizard can be bound to an MCP source', async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}><SourceList /></NextIntlClientProvider>
      </QueryClientProvider>);
    await screen.findByText('Mcp one');
    expect(screen.getByRole('button', { name: 'Configure' })).toBeTruthy();
  });
});
