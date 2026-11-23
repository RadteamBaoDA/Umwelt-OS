import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import type { GadgetInstance } from '../api';
import { NewsFeed } from './news-feed';

const mocks = vi.hoisted(() => ({
  isOwner: true,
  openDrawer: vi.fn(),
  stories: [] as Array<Record<string, unknown>>,
}));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ openDrawer: mocks.openDrawer }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isOwner: mocks.isOwner }) }));
vi.mock('@/modules/news', async () => {
  const { StoryListProbe } = await import('./news-feed-test-support');
  return { StoryList: (props: Parameters<typeof StoryListProbe>[0]) => <StoryListProbe {...props} stories={mocks.stories} /> };
});

/** Minimal News gadget instance; only the fields NewsFeed reads are populated. */
const instance = {
  id: 'news-1',
  definition: { source_ids: ['s1'], filters: {}, scope: {}, highlight_rules: [] },
} as unknown as GadgetInstance;

const evidence = (n: number) => ({
  source_id: `src-${n}`,
  document_id: `doc-${n}`,
  document_version_id: `ver-${n}`,
});

/** Renders the News gadget with the shared messages and a query client. */
function renderFeed() {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <NewsFeed instance={instance} />
      </QueryClientProvider>
    </NextIntlClientProvider>,
  );
}

describe('NewsFeed story Ask AI', () => {
  beforeEach(() => {
    mocks.isOwner = true;
    mocks.openDrawer.mockReset();
    mocks.stories = [];
  });

  it('anchors Ask AI on the first evidence item of the story', async () => {
    mocks.stories = [{ id: 'st-1', title: 'Story', evidence: [evidence(1), evidence(2)] }];
    renderFeed();
    const row = (await screen.findByText('Story')).closest('li') as HTMLElement;
    await userEvent.click(within(row).getByRole('button', { name: 'Ask AI' }));
    expect(mocks.openDrawer).toHaveBeenCalledWith({
      context: {
        kind: 'selection',
        anchor: true,
        items: [{ sourceId: 'src-1', documentId: 'doc-1', documentVersionId: 'ver-1' }],
      },
    });
  });

  it('renders no Ask AI button for a story without evidence', async () => {
    mocks.stories = [{ id: 'st-2', title: 'Bare story', evidence: [] }];
    renderFeed();
    expect(await screen.findByText('Bare story')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Ask AI' })).toBeNull();
  });
});
