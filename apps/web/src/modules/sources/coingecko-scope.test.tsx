import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import { ProviderScope } from './provider-scope';
import type { ConnectorConfig } from './api';

vi.mock('./api', async (importOriginal) => ({ ...await importOriginal<typeof import('./api')>(), searchCoinGeckoCoins: vi.fn() }));

import { searchCoinGeckoCoins } from './api';

const bitcoin = { id: 'bitcoin', symbol: 'BTC', name: 'Bitcoin' };
const ether = { id: 'ethereum', symbol: 'ETH', name: 'Ethereum' };

/** Mounts the controlled picker and applies each edit to its source configuration. */
function mount(initial?: typeof bitcoin[]) {
  const config: ConnectorConfig = { timeout_seconds: 30, timezone: 'UTC', schedule_interval_minutes: 15, ...(initial ? { coingecko_assets: initial } : {}) };
  const changes: typeof bitcoin[][] = [];
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = (next: ConnectorConfig) => <QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
    <ProviderScope provider="coingecko" sourceId="source-1" credentialConfigured configuration={next} disabled={false} resetEpoch={0} onChange={(_key, value) => {
      const assets = value as typeof bitcoin[];
      changes.push(assets);
      rerender(view({ ...next, coingecko_assets: assets }));
    }} />
  </NextIntlClientProvider></QueryClientProvider>;
  const { rerender } = render(view(config));
  return { changes, rerender };
}

describe('CoinGecko source scope', () => {
  it('searches the catalog and stores multiple IDs without losing Bitcoin', async () => {
    vi.mocked(searchCoinGeckoCoins).mockResolvedValue({ items: [ether] });
    const { changes } = mount();
    await userEvent.type(screen.getByLabelText('Coins to collect (USD)'), 'eth');
    await screen.findByRole('button', { name: /Ethereum \(ETH\)/ });
    await userEvent.click(screen.getByRole('button', { name: /Ethereum \(ETH\)/ }));
    expect(changes.at(-1)).toEqual([bitcoin, ether]);
    expect(screen.getByRole('list', { name: 'Selected coins' }).textContent).toContain('ethereum');
  });

  it('keeps selected assets when catalog search fails', async () => {
    vi.mocked(searchCoinGeckoCoins).mockRejectedValue(new Error('offline'));
    const { changes } = mount([bitcoin, ether]);
    await userEvent.type(screen.getByLabelText('Coins to collect (USD)'), 'sol');
    expect(await screen.findByRole('alert')).toHaveTextContent('Your selected coins are still here');
    expect(screen.getByRole('list', { name: 'Selected coins' }).textContent).toContain('ethereum');
    await waitFor(() => expect(changes).toEqual([]));
  });
});
