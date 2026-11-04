import { cleanup, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { IngestionRun, Source } from './api';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/error-codes', () => ({ useErrorCodeLabel: () => (code: string) => code }));
const api = vi.hoisted(() => ({ getSourceIngestion: vi.fn(), retryRun: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

import { SyncHistory } from './sync-history';

const run = (stats: IngestionRun['collection_stats']): IngestionRun => ({
  run_id: 'aaaaaaaa-1', source_id: 's', status: 'succeeded', stages: [], error_code: null, created_at: '2026-11-02T00:00:00Z', updated_at: '2026-11-02T00:00:00Z', collection_stats: stats,
});
const view = async (stats: IngestionRun['collection_stats']) => {
  api.getSourceIngestion.mockResolvedValue({ current_run: null, items: [run(stats)], next_cursor: null });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}><SyncHistory source={{ id: 's', status: 'active', provider: 'tuoitre_news', last_success_at: null } as unknown as Source} /></NextIntlClientProvider>
  </QueryClientProvider>);
  await screen.findByText('aaaaaaaa');
};
afterEach(() => cleanup());

describe('SyncHistory collection stats', () => {
  it('shows new, backlog, gap, duplicates and failed sections', async () => {
    await view({ new: 5, backlog: 12, gap: ['2026-10-30T00:00:00Z', '2026-11-01T00:00:00Z'], duplicates: 3, failed_sections: ['world', 'tech'] });
    expect(screen.getByText('5 new · 12 waiting for next sync')).toBeTruthy();
    expect(screen.getByText(/^Gap:/)).toBeTruthy();
    expect(screen.getByText('3 duplicates skipped')).toBeTruthy();
    expect(screen.getByText('Sections that could not be fetched: world, tech')).toBeTruthy();
  });

  it('omits the stats block when the run has none', async () => {
    await view(null);
    expect(screen.queryByText(/waiting for next sync/)).toBeNull();
  });

  it('shows truncated, failed MCP calls and the catching-up note', async () => {
    await view({ new: 1, truncated: true, failed_calls: 2, caught_up: false });
    expect(screen.getByText(/Page limit reached/)).toBeTruthy();
    expect(screen.getByText('2 tool calls failed')).toBeTruthy();
    expect(screen.getByText(/catching up on older posts/)).toBeTruthy();
  });

  it('omits the new lines when absent or already caught up', async () => {
    await view({ new: 1, caught_up: true });
    expect(screen.queryByText(/Page limit reached/)).toBeNull();
    expect(screen.queryByText(/tool calls failed/)).toBeNull();
    expect(screen.queryByText(/catching up on older posts/)).toBeNull();
  });
});
