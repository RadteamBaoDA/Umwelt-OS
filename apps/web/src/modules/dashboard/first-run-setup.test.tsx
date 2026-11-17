import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

const mocks = vi.hoisted(() => ({ apiRequest: vi.fn(), fetchTopics: vi.fn(), getDailyContext: vi.fn() }));
vi.mock('@/core/api', () => ({ retrySessionConflict: () => false, apiRequest: mocks.apiRequest }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/modules/news/api', () => ({ fetchTopics: mocks.fetchTopics }));
vi.mock('./daily-api', () => ({
  dailyKeys: { context: (d: string, z: string) => ['c', d, z] },
  generateBrief: vi.fn(),
  getDailyContext: mocks.getDailyContext,
}));

import { FirstRunSetup } from './first-run-setup';

/** Renders the panel with English messages and a fresh query cache. */
function renderPanel(sourceCount: number, onOpenStarterPacks: () => void = () => {}) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <FirstRunSetup sourceCount={sourceCount} csrfToken="t" onPickTopics={() => {}} onOpenStarterPacks={onOpenStarterPacks} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

describe('FirstRunSetup', () => {
  beforeEach(() => {
    mocks.apiRequest.mockResolvedValue({ providers: [] });
    mocks.fetchTopics.mockResolvedValue({ items: [], next_cursor: null });
    mocks.getDailyContext.mockResolvedValue({ brief: null });
  });
  it('shows the three steps with real state: models link when no provider', async () => {
    renderPanel(0);
    expect(screen.getByText('Set up your first brief')).toBeTruthy();
    expect(await screen.findByRole('link', { name: 'Add a model first' })).toBeTruthy();
    expect(screen.queryByText('Done')).toBeNull();
  });
  it('opens the starter-pack wizard in place instead of linking away', async () => {
    const onOpen = vi.fn();
    renderPanel(0, onOpen);
    const button = screen.getByRole('button', { name: 'Connect a source' });
    button.click();
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('link', { name: 'Connect a source' })).toBeNull();
    await screen.findByRole('link', { name: 'Add a model first' });
  });
  it('marks steps done from topics, source count and brief', async () => {
    mocks.fetchTopics.mockResolvedValue({ items: [{ id: 't' }], next_cursor: null });
    mocks.getDailyContext.mockResolvedValue({ brief: { id: 'b' } });
    renderPanel(1);
    await waitFor(() => expect(screen.getAllByText('Done')).toHaveLength(3));
  });
});
