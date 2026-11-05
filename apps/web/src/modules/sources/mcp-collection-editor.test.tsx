import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'Europe/Paris' }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
const api = vi.hoisted(() => ({ getConnectorConfiguration: vi.fn(), activateConnector: vi.fn(), updateSourceStatus: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));
const mcp = vi.hoisted(() => ({ listMcpConnections: vi.fn(), listMcpGrants: vi.fn(), saveMcpCollection: vi.fn() }));
vi.mock('./mcp-api', async (original) => ({ ...(await original<typeof import('./mcp-api')>()), ...mcp }));

import { McpCollectionEditor } from './mcp-collection-editor';

const wrap = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
      <McpCollectionEditor sourceId="s1" sourceStatus="active" onChanged={() => undefined} onDraftChange={() => undefined} />
    </NextIntlClientProvider>
  </QueryClientProvider>);

// Restore spies even when an assertion throws so mocks never leak into later tests.
afterEach(() => vi.restoreAllMocks());

beforeEach(() => {
  vi.clearAllMocks();
  mcp.listMcpConnections.mockResolvedValue({ items: [{ id: 'c1', name: 'Conn', enabled: true, revision: 1, deployment_profile_hash: 'h' }] });
  mcp.listMcpGrants.mockResolvedValue({ items: [] });
});

describe('McpCollectionEditor', () => {
  it('defaults the timezone to the browser zone instead of a fixed region and passes an abort signal', async () => {
    api.getConnectorConfiguration.mockResolvedValue({ source_generation: 1, configuration: { connection_id: 'c1', calls: [] } });
    wrap();
    const input = await (async () => { await screen.findByText('Save & enable'); return document.getElementById('mcp-collection-timezone')!; })();
    await waitFor(() => expect((input as HTMLInputElement).value).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone));
    expect(api.getConnectorConfiguration.mock.calls[0]?.[1]).toBeInstanceOf(AbortSignal);
  });

  it('keeps a saved timezone', async () => {
    api.getConnectorConfiguration.mockResolvedValue({ source_generation: 1, configuration: { connection_id: 'c1', calls: [], timezone: 'Asia/Tokyo' } });
    wrap();
    const input = await (async () => { await screen.findByText('Save & enable'); return document.getElementById('mcp-collection-timezone')!; })();
    await waitFor(() => expect((input as HTMLInputElement).value).toBe('Asia/Tokyo'));
  });
});

describe('McpCollectionEditor browser timezone default', () => {
  it('uses the browser zone, not the (possibly still bootstrap) preference zone, when none is saved', async () => {
    vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions').mockReturnValue({ timeZone: 'America/Denver' } as Intl.ResolvedDateTimeFormatOptions);
    api.getConnectorConfiguration.mockResolvedValue({ source_generation: 1, configuration: { connection_id: 'c1', calls: [] } });
    wrap();
    await screen.findByText('Save & enable');
    await waitFor(() => expect((document.getElementById('mcp-collection-timezone') as HTMLInputElement).value).toBe('America/Denver'));
  });
});

describe('McpCollectionEditor load-error acknowledgement', () => {
  it('shows a later grants failure for another connection after a successful save', async () => {
    mcp.listMcpConnections.mockResolvedValue({ items: [
      { id: 'c1', name: 'Conn', enabled: true, revision: 1, deployment_profile_hash: 'h' },
      { id: 'c2', name: 'Other', enabled: true, revision: 1, deployment_profile_hash: 'h' }] });
    api.getConnectorConfiguration.mockResolvedValue({ source_generation: 1, configuration: { connection_id: 'c1', calls: [{ grant_id: 'g1', arguments: {} }] } });
    mcp.saveMcpCollection.mockResolvedValue({ source_generation: 2, expected_revision: 1 });
    mcp.listMcpGrants.mockImplementation(async (id: string) => { if (id === 'c2') throw new Error('boom'); return { items: [] }; });
    const user = userEvent.setup();
    wrap();
    await user.click(await screen.findByRole('button', { name: 'Save configuration' }));
    await waitFor(() => expect(mcp.saveMcpCollection).toHaveBeenCalled());
    expect(screen.queryByRole('alert')).toBeNull();
    await user.click(screen.getByRole('combobox', { name: 'MCP connection' }));
    await user.click(await screen.findByRole('option', { name: 'Other' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(sourcesMessages['en-us'].mcpCollectionSaveFailed);
  });
});
