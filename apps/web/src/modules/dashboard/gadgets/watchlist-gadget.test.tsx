import { render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { dailyMessages } from '../daily-messages';
import { WatchlistGadget, type WatchlistItem } from './watchlist-gadget';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

const item = (over: Partial<WatchlistItem>): WatchlistItem => ({
  id: 'x', target: 'T', targetType: 'keyword', condition: 'c', status: 'monitoring', severity: 'info', lastMatchedAt: null, matchCount: 0, ...over,
});
const items = [
  item({ id: '1', target: 'Quiet rule' }),
  item({ id: '2', target: 'Critical alert', status: 'alert', severity: 'critical', lastMatchedAt: new Date(Date.now() - 3_600_000).toISOString(), matchCount: 4 }),
  item({ id: '3', target: 'Matched rule', status: 'matched', severity: 'warning', lastMatchedAt: new Date(Date.now() - 7_200_000).toISOString(), matchCount: 1 }),
];

describe('WatchlistGadget', () => {
  it('shows a localized summary, no hard-coded English headers, and lists alerting rules first', () => {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], daily: dailyMessages['en-us'] }}>
          <WatchlistGadget instance={{ id: 'g', title: '', definition: { renderer: 'watch_rules', source_ids: [], highlight_rules: [] } } as never} initialItems={items} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    expect(screen.getByText('1 alerting · 3 rules')).toBeTruthy();
    expect(screen.queryByText('Condition / Trigger')).toBeNull();
    expect(screen.getByText('Never matched')).toBeTruthy();
    const rows = within(screen.getByRole('list')).getAllByRole('listitem');
    expect(rows[0].textContent).toContain('Critical alert');
    expect(rows[0].textContent).toContain('4×');
    expect(rows[2].textContent).toContain('Quiet rule');
  });
});
