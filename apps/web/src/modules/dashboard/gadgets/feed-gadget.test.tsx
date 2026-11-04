import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { FeedGadget, type FeedStreamItem } from './feed-gadget';

vi.mock('@/modules/knowledge/api', () => ({
  documentKeys: { all: ['documents'] },
  listGadgetDocumentProjections: vi.fn(async () => ({ items: [], next_cursor: null })),
  setGadgetDocumentInteraction: vi.fn(),
}));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ openDrawer: vi.fn() }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 't' }) }));

const hour = 3_600_000;
const items: FeedStreamItem[] = [
  { id: 'a', title: 'Lead headline', sourceType: 'news', author: 'Wire', excerpt: 'Lead excerpt text', timestamp: new Date(Date.now() - 2 * hour).toISOString(), read: false },
  { id: 'b', title: 'Second headline', sourceType: 'news', timestamp: new Date(Date.now() - 30 * hour).toISOString(), read: true },
  { id: 'c', title: 'Third headline', sourceType: 'news', timestamp: new Date(Date.now() - 55 * hour).toISOString(), read: true },
];

function view() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const instance = { id: 'g1', title: '', definition: { renderer: 'feed', source_ids: ['s1'], scope: {} } } as never;
  return render(
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}>
        <FeedGadget instance={instance} initialItems={items} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

describe('FeedGadget lead + list', () => {
  it('renders the first item as the lead and the rest as compact rows without an Unread pill', () => {
    view();
    const lead = screen.getByTestId('feed-lead');
    expect(within(lead).getByText('Lead headline')).toBeTruthy();
    expect(within(lead).getByText('Lead excerpt text')).toBeTruthy();
    expect(screen.getByText('Second headline').closest('li')).toBeTruthy();
    // Unread is a dot with screen-reader text, not a visible pill; read items carry none.
    expect(screen.getAllByText('1 unread')).toHaveLength(1);
    expect(screen.queryByRole('checkbox')).toBeNull();
  });

  it('collapses the filters behind one Filter popover', async () => {
    view();
    expect(screen.queryByRole('searchbox')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Filter' }));
    expect(await screen.findByRole('searchbox')).toBeTruthy();
  });
});
