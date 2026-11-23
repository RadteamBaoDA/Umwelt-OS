import userEvent from '@testing-library/user-event';
import { render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';

import { WatchlistGadget, type WatchlistItem } from './watchlist-gadget';

const windowMock = vi.hoisted(() => vi.fn());
const mocks = vi.hoisted(() => ({ openDrawer: vi.fn() }));
vi.mock('@/modules/observations/api', () => ({ listWorldObservationWindow: windowMock }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ openDrawer: mocks.openDrawer }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isOwner: true }) }));

const item = (over: Partial<WatchlistItem>): WatchlistItem => ({
  id: 'x',
  target: 'T',
  targetType: 'keyword',
  condition: 'c',
  status: 'monitoring',
  severity: 'info',
  lastMatchedAt: null,
  matchCount: 0,
  ...over,
});
const items = [
  item({ id: '1', target: 'Quiet rule' }),
  item({
    id: '2',
    target: 'Critical alert',
    status: 'alert',
    severity: 'critical',
    lastMatchedAt: new Date(Date.now() - 3_600_000).toISOString(),
    matchCount: 4,
  }),
  item({
    id: '3',
    target: 'Matched rule',
    status: 'matched',
    severity: 'warning',
    lastMatchedAt: new Date(Date.now() - 7_200_000).toISOString(),
    matchCount: 1,
  }),
];

describe('WatchlistGadget', () => {
  it('shows a localized summary, no hard-coded English headers, and lists alerting rules first', () => {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <NextIntlClientProvider
          locale="en-US"
          messages={{ dashboard: getCatalog('en-us', 'dashboard'), daily: getCatalog('en-us', 'daily') }}
        >
          <WatchlistGadget
            instance={{ id: 'g', title: '', definition: { renderer: 'watch_rules', source_ids: [], highlight_rules: [] } } as never}
            initialItems={items}
          />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    // Compact summary: the hit count is the big figure, the rule total a muted tail.
    expect(document.querySelector('.watch-figure')?.textContent).toBe('2');
    expect(document.querySelector('.watch-figure-tail')?.textContent).toContain('3');
    expect(screen.queryByText('Condition / Trigger')).toBeNull();
    const rows = within(screen.getByRole('list')).getAllByRole('listitem');
    expect(rows[0].textContent).toContain('Critical alert');
    expect(rows[0].textContent).toContain('HIT');
    expect(rows[2].textContent).toContain('Quiet rule');
  });

  const obs = (observed_at: string, value: number) => ({
    id: observed_at,
    source_id: 's1',
    provider: 'coingecko',
    metric: 'price',
    symbol: 'BITCOIN',
    observed_at,
    value,
    currency: 'USD',
    unit: 'USD',
  });
  const renderWith = (list: WatchlistItem[]) =>
    render(
      <QueryClientProvider client={new QueryClient()}>
        <NextIntlClientProvider
          locale="en-US"
          messages={{ dashboard: getCatalog('en-us', 'dashboard'), daily: getCatalog('en-us', 'daily') }}
        >
          <WatchlistGadget
            instance={{ id: 'g', title: '', definition: { renderer: 'watch_rules', source_ids: ['s1'], highlight_rules: [] } } as never}
            initialItems={list}
          />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );

  it('renders a sparkline row for a symbol matched by alias with one batched query; keyword rows are unchanged', async () => {
    windowMock.mockReset();
    windowMock.mockResolvedValue({ truncated: false, items: [obs('2026-01-01T00:00:00Z', 100), obs('2026-01-02T00:00:00Z', 110)] });
    renderWith([
      item({ id: 'a', target: 'btc', targetType: 'symbol' }),
      item({ id: 'b', target: 'eth', targetType: 'symbol' }),
      item({ id: 'k', target: 'Keyword rule' }),
    ]);
    const row = await screen.findByTestId('watchlist-symbol-row');
    expect(row.textContent).toContain('btc');
    expect(row.textContent).toContain('$110');
    expect(row.querySelector('svg')).toBeTruthy();
    await waitFor(() => expect(screen.getByText('Keyword rule')).toBeTruthy());
    expect(screen.getAllByTestId('watchlist-symbol-row')).toHaveLength(1);
    expect(windowMock).toHaveBeenCalledTimes(1);
    expect(windowMock.mock.calls[0][0].symbols).toEqual(['BITCOIN', 'BTC', 'ETH', 'ETHEREUM']);
  });
});

describe('WatchlistGadget row Ask AI', () => {
  it('shows Ask AI only on rows that carry a full document identity and anchors it', async () => {
    mocks.openDrawer.mockReset();
    const withIdentity = item({
      id: 'w1',
      target: 'Identified hit',
      status: 'matched',
      severity: 'warning',
      sourceId: 's1',
      documentId: 'd1',
      documentVersionId: 'v1',
    });
    const withoutIdentity = item({ id: 'w2', target: 'Plain hit', status: 'matched' });
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <NextIntlClientProvider
          locale="en-US"
          messages={{ dashboard: getCatalog('en-us', 'dashboard'), daily: getCatalog('en-us', 'daily') }}
        >
          <WatchlistGadget
            instance={{ id: 'g', title: '', definition: { renderer: 'watch_rules', source_ids: [], highlight_rules: [] } } as never}
            initialItems={[withIdentity, withoutIdentity]}
          />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    const buttons = screen.getAllByRole('button', { name: 'Ask AI' });
    expect(buttons).toHaveLength(1);
    await userEvent.click(buttons[0]!);
    expect(mocks.openDrawer).toHaveBeenCalledWith({
      context: { kind: 'selection', anchor: true, items: [{ sourceId: 's1', documentId: 'd1', documentVersionId: 'v1' }] },
    });
  });
});
