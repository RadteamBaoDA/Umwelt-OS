import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { StoryList } from './story-list';
import { fetchNewsTrends, fetchStories } from './story-api';

vi.mock('./story-api', () => ({
  fetchStories: vi.fn(async () => ({ items: [], next_cursor: null, incomplete_reasons: [] })),
  fetchNewsTrends: vi.fn(async () => ({ items: [], incomplete_reasons: [] })),
}));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-US', timezone: 'UTC' }) }));
vi.mock('@/modules/translations/use-content-translation', () => ({ useContentTranslation: () => ({ results: new Map(), issues: new Map() }) }));

const renderList = (noSources?: boolean) => render(
  <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <StoryList sourceIds={noSources ? [] : ['s1']} noSources={noSources} onSelectStory={() => undefined} />
    </QueryClientProvider>
  </NextIntlClientProvider>,
);

describe('story list empty state', () => {
  it('points to source setup when no sources are configured', async () => {
    renderList(true);
    const link = await screen.findByRole('link', { name: 'Add a news source' });
    expect(link.getAttribute('href')).toBe('/settings/sources');
  });

  it('offers refresh when sources exist but no stories were collected', async () => {
    renderList(false);
    expect(await screen.findByText('Stories appear after the next collection.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Add a news source' })).toBeNull();
  });
});

describe('story list embedded lead', () => {
  it('leads with the story that has the most sources and shows trends as chips', async () => {
    const story = (id: string, title: string, source_count: number) => ({
      id, title, excerpt: `${title} excerpt`, source_count, evidence_count: source_count, observed_at: '2026-11-03T10:00:00Z',
      relevance: null, incomplete_reasons: [], evidence: [], translation_revision: null,
    });
    vi.mocked(fetchStories).mockResolvedValueOnce({
      items: [story('s-a', 'Small story', 1), story('s-b', 'Big story', 6)], next_cursor: null, incomplete_reasons: [],
    } as never);
    vi.mocked(fetchNewsTrends).mockResolvedValueOnce({
      items: [{ story_id: 's-b', title: 'Rising thing', current_count: 5, ratio: 2.5, low_baseline: false }], incomplete_reasons: [],
    } as never);
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <StoryList sourceIds={['s1']} embedded onSelectStory={() => undefined} />
        </QueryClientProvider>
      </NextIntlClientProvider>,
    );
    const lead = await screen.findByTestId('news-lead');
    expect(lead.textContent).toContain('Big story');
    expect(lead.textContent).toContain('6');
    expect(lead.textContent).toContain('sources');
    expect(screen.getByText('Small story')).toBeTruthy();
    expect(await screen.findByText('2.5×')).toBeTruthy();
  });

  it('hides the big figure for a one-source lead and shows plain meta text', async () => {
    vi.mocked(fetchStories).mockResolvedValueOnce({
      items: [{ id: 's-1', title: 'Solo story', excerpt: '', source_count: 1, evidence_count: 1, observed_at: '2026-11-03T10:00:00Z',
        relevance: null, incomplete_reasons: [], evidence: [], translation_revision: null }], next_cursor: null, incomplete_reasons: [],
    } as never);
    vi.mocked(fetchNewsTrends).mockResolvedValueOnce({ items: [], incomplete_reasons: [] } as never);
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <StoryList sourceIds={['s1']} embedded onSelectStory={() => undefined} />
        </QueryClientProvider>
      </NextIntlClientProvider>,
    );
    const lead = await screen.findByTestId('news-lead');
    expect(lead.textContent).toContain('1 source');
    expect(lead.querySelector('.text-figure-sm')).toBeNull();
  });
});
