import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

let search = '';
const replace = vi.fn();
vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams(search), useRouter: () => ({ replace }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isOwner: false }) }));
vi.mock('@/modules/sources', () => ({ listSources: vi.fn() }));
vi.mock('./story-list', () => ({ StoryList: () => null }));
vi.mock('./story-detail', () => ({
  StoryDetail: ({ storyId }: { storyId: string }) => <div data-testid="story-detail">{storyId}</div>,
}));

import { NewsPage } from './news-page';

const view = () => (
  <QueryClientProvider client={new QueryClient()}>
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <NewsPage />
    </NextIntlClientProvider>
  </QueryClientProvider>
);

describe('NewsPage ?story deep link', () => {
  beforeEach(() => vi.clearAllMocks());

  it('opens the story named by ?story', () => {
    search = 'story=abc';
    render(view());
    expect(screen.getByTestId('story-detail')).toHaveTextContent('abc');
  });

  it('keeps the dialog closed without the param', () => {
    search = '';
    render(view());
    expect(screen.queryByTestId('story-detail')).toBeNull();
  });
});
