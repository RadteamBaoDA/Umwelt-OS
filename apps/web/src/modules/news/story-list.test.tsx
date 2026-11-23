import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
const translationState = vi.hoisted(() => ({ enabled: false }));
vi.mock('@/modules/translations/use-content-translation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/modules/translations/use-content-translation')>()),
  useContentTranslation: () => ({
    enabled: translationState.enabled,
    targetLanguage: 'vi',
    results: new Map(),
    issues: new Map(),
    unchanged: new Set(),
    pendingIds: new Set(),
    translate: vi.fn(),
  }),
}));

const renderList = (noSources?: boolean) =>
  render(
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

describe('story list embedded log', () => {
  it('renders the embedded list as log rows (no lead hero) and shows trends as chips', async () => {
    const story = (id: string, title: string, source_count: number) => ({
      id,
      title,
      excerpt: `${title} excerpt`,
      source_count,
      evidence_count: source_count,
      observed_at: '2026-11-03T10:00:00Z',
      relevance: null,
      incomplete_reasons: [],
      evidence: [],
      translation_revision: null,
    });
    vi.mocked(fetchStories).mockResolvedValueOnce({
      items: [story('s-a', 'Small story', 1), story('s-b', 'Big story', 6)],
      next_cursor: null,
      incomplete_reasons: [],
    } as never);
    vi.mocked(fetchNewsTrends).mockResolvedValueOnce({
      items: [{ story_id: 's-b', title: 'Rising thing', current_count: 5, ratio: 2.5, low_baseline: false }],
      incomplete_reasons: [],
    } as never);
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <StoryList sourceIds={['s1']} embedded onSelectStory={() => undefined} />
        </QueryClientProvider>
      </NextIntlClientProvider>,
    );
    expect(await screen.findAllByTestId('news-log-row')).toHaveLength(2);
    expect(screen.queryByTestId('news-lead')).toBeNull();
    expect(screen.getByText('Small story')).toBeTruthy();
    expect(await screen.findByText('2.5×')).toBeTruthy();
  });
});

describe('story list embedded feed features', () => {
  const mk = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      id: `s-${i}`,
      title: `Story ${i}`,
      excerpt: '',
      source_count: 1,
      evidence_count: 1,
      observed_at: '2026-11-03T10:00:00Z',
      relevance: null,
      incomplete_reasons: [],
      evidence: [],
      translation_revision: null,
      image_url: i === 0 ? '/api/v1/media/proxy?token=x' : null,
    }));
  const renderEmbedded = () =>
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <StoryList
            sourceIds={['a', 'b']}
            sourceNames={[
              { id: 'a', name: 'Alpha' },
              { id: 'b', name: 'Beta' },
            ]}
            embedded
            instanceId="g1"
            onSelectStory={() => undefined}
          />
        </QueryClientProvider>
      </NextIntlClientProvider>,
    );

  it('shows 5 then expands, switches to image cards, and scopes stories to the chosen source tab', async () => {
    vi.mocked(fetchStories).mockResolvedValue({ items: mk(9), next_cursor: null, incomplete_reasons: [] } as never);
    renderEmbedded();
    expect(await screen.findAllByTestId('news-log-row')).toHaveLength(8); // log rows, eight before "show more"
    fireEvent.click(screen.getByRole('button', { name: /^Show \d+ more$/ }));
    expect(screen.getAllByTestId('news-log-row')).toHaveLength(9);
    fireEvent.click(screen.getAllByRole('button', { name: 'Cards' })[0]);
    expect((await screen.findAllByTestId('news-card')).length).toBe(6);
    expect(document.querySelector('img[src^="/api/v1/media/proxy"]')).toBeTruthy();
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Beta' }));
    await waitFor(() => expect(vi.mocked(fetchStories).mock.calls.at(-1)?.[0]).toEqual(['b']));
    window.localStorage.clear();
  });
});

describe('story list log translation', () => {
  it('keeps per-story Translate reachable and leaves Translate all to the gadget menu in the log view', async () => {
    translationState.enabled = true;
    try {
      vi.mocked(fetchStories).mockResolvedValue({
        items: [
          {
            id: 's-1',
            title: 'Log story',
            excerpt: '',
            source_count: 1,
            evidence_count: 1,
            observed_at: '2026-11-03T10:00:00Z',
            relevance: null,
            incomplete_reasons: [],
            evidence: [],
            translation_revision: 'r1',
          },
        ],
        next_cursor: null,
        incomplete_reasons: [],
      } as never);
      render(
        <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
          <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
            <StoryList sourceIds={['s1']} embedded onSelectStory={() => undefined} />
          </QueryClientProvider>
        </NextIntlClientProvider>,
      );
      const row = await screen.findByTestId('news-log-row');
      expect(row.querySelector('button[aria-label="Translate “Log story”"]')).toBeTruthy();
      expect(document.querySelector('.feed-view-narrow')?.textContent).not.toContain('Translate all');
    } finally {
      translationState.enabled = false;
      window.localStorage.clear();
    }
  });
});
