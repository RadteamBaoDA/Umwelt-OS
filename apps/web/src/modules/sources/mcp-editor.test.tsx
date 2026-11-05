import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import mcpCatalog from './mcp-messages.json';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'Europe/Paris' }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
vi.mock('@/core/guarded-navigation', () => ({ useGuardedNavigation: () => ({ registerLeaveGuard: () => () => undefined }) }));
const api = vi.hoisted(() => ({ listSources: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));
const mcp = vi.hoisted(() => ({ listMcpConnections: vi.fn(), listMcpInboundClients: vi.fn(), listMcpNativeTools: vi.fn(), listMcpGrants: vi.fn() }));
vi.mock('./mcp-api', async (original) => ({ ...(await original<typeof import('./mcp-api')>()), ...mcp }));

import { McpSettings } from './mcp-editor';

const connection = { id: 'c1', name: 'Docs server', transport: 'streamable_http', enabled: false, revision: 3, endpoint: 'https://mcp.example.test', auth_method: 'none', timeout_seconds: 10, health: 'connected', error_code: null, updated_at: '2026-01-01T00:00:00Z', credential_configured: false, deployment_profile_id: null, deployment_profile_hash: null };

const wrap = () => render(
  <NextIntlClientProvider locale="en-US" messages={{ mcp: (mcpCatalog as Record<string, Record<string, string>>)['en-us'] }}>
    <McpSettings />
  </NextIntlClientProvider>);

beforeEach(() => {
  vi.clearAllMocks();
  mcp.listMcpConnections.mockResolvedValue({ items: [connection] });
  mcp.listMcpInboundClients.mockResolvedValue({ items: [] });
  mcp.listMcpNativeTools.mockResolvedValue({ items: [] });
  mcp.listMcpGrants.mockResolvedValue({ items: [] });
  api.listSources.mockResolvedValue({ items: [], next_cursor: null });
});

describe('McpSettings', () => {
  it('loads the snapshot, selects the first connection and shows its saved health and actions', async () => {
    wrap();
    expect(await screen.findByRole('heading', { name: 'Docs server', level: 4 })).toBeTruthy();
    expect(screen.getByText(/Saved health/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Check saved connection' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Find tools' })).toBeTruthy();
    // Not yet discovered and reviewed, so the connection cannot be enabled.
    expect((screen.getByRole('button', { name: 'Enable' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'Selected' })).toBeTruthy();
  });

  it('opens an empty draft for a new connection and marks the draft retained after typing', async () => {
    wrap();
    await screen.findByRole('button', { name: 'New connection' });
    await waitFor(() => expect((screen.getByRole('button', { name: 'New connection' }) as HTMLButtonElement).disabled).toBe(false));
    await userEvent.click(screen.getByRole('button', { name: 'New connection' }));
    const name = await screen.findByLabelText('Name', { selector: '#mcp-name' });
    await userEvent.type(name, 'Mine');
    expect((name as HTMLInputElement).value).toBe('Mine');
    expect(screen.getByText('Your unsaved draft is preserved for review.')).toBeTruthy();
    expect(screen.getByRole('group', { name: 'Connection draft' })).toBeTruthy();
  });

  it('keeps the inbound clients section collapsed with its issue form in the DOM', async () => {
    wrap();
    await screen.findByRole('heading', { name: 'Docs server', level: 4 });
    expect(document.getElementById('inbound-name')).not.toBeNull();
    expect(document.getElementById('inbound-expiry')).not.toBeNull();
  });
});
