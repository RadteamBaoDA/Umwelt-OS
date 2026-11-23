import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';
import { VideoPanel } from './video-panel';

const mocks = vi.hoisted(() => ({ list: vi.fn(), interact: vi.fn(), ask: vi.fn(), submit: vi.fn() }));
// Translation api: settings enabled so the collection bar renders; submit records what "Translate all" sends.
vi.mock('../../translations/api', () => ({
  fetchTranslationSettings: vi.fn(async () => ({ enabled: true, target_language: 'vi', configuration_revision: 1 })),
  submitTranslationBatch: mocks.submit,
  readTranslationBatch: vi.fn(),
}));
vi.mock('@/modules/knowledge', () => ({
  documentKeys: { all: ['documents'] },
  listGadgetDocumentProjections: mocks.list,
  setGadgetDocumentInteraction: mocks.interact,
}));
vi.mock('../api', () => ({
  dashboardKeys: { sources: ['sources'] },
  listGadgetSources: vi.fn(async () => ({
    items: [
      { id: 's1', name: 'Feed one' },
      { id: 's2', name: 'Feed two' },
    ],
  })),
}));
vi.mock('../gadget-freshness', () => ({ useReportGadgetFreshness: () => undefined }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ openDrawer: mocks.ask }) }));
vi.mock('@/core/app-shell/workspace-session', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isOwner: true, selection: { id: 'w1' } }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-US', timezone: 'UTC' }) }));

const projection = (id: string, source = 's1') => ({
  document_id: id,
  document_version_id: `${id}-v1`,
  version_number: 1,
  source_id: source,
  title: `Media ${id}`,
  canonical_url: `https://media.example/${id}`,
  published_at: '2026-01-02T03:04:00Z',
  observed_at: '2026-01-02T03:04:00Z',
  excerpt: 'Projection excerpt',
  metadata_is_version_snapshot: true,
  image_url: null,
  media: { creator: 'Creator', duration_seconds: 125, media_type: 'video' },
  read_at: null,
  bookmarked_at: null,
  dismissed_at: null,
  provider_metadata: { provider: 'arbitrary-provider', source_fields: {} },
});

/** Renders the gadget with its query and locale providers for focused behavior checks. */
function renderPanel(sourceIds = ['s1', 's2']) {
  const instance = { id: 'v', definition: { renderer: 'video_panel', source_ids: sourceIds, scope: {} } } as never;
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <NextIntlClientProvider
        locale="en-US"
        messages={{
          dashboard: getCatalog('en-us', 'dashboard'),
          daily: { streamCount: '{count}' },
          translations: getCatalog('en-us', 'translations'),
        }}
      >
        <VideoPanel instance={instance} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

describe('VideoPanel', () => {
  beforeEach(() => {
    mocks.list.mockReset();
    mocks.interact.mockReset().mockResolvedValue({});
    mocks.ask.mockReset();
    mocks.submit.mockReset();
  });

  it('accepts media projections regardless of provider or URL hostname and shows creator, time and duration', async () => {
    mocks.list.mockResolvedValue({ items: [projection('hero'), projection('a')], next_cursor: null });
    renderPanel(['s1']);
    expect(await screen.findByText('Media a')).toBeTruthy();
    // The first video is the hero frame; the second reads as a list row with creator, time and duration.
    expect(screen.getAllByText(/Creator/).length).toBeGreaterThan(0);
    expect(screen.getByText(/3 min/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Media a' }).getAttribute('href')).toBe('https://media.example/a');
  });

  it('falls back to the document detail route when a projected external URL is unsafe', async () => {
    mocks.list.mockResolvedValue({ items: [{ ...projection('a'), canonical_url: 'javascript:alert(1)' }], next_cursor: null });
    renderPanel(['s1']);
    const link = await screen.findByRole('link', { name: 'Media a' });
    expect(link.getAttribute('href')).toBe('/knowledge/documents/a');
    expect(link.hasAttribute('target')).toBe(false);
  });

  it('keeps source selection and opaque cursor pagination server-scoped', async () => {
    mocks.list.mockImplementation(async (sources: string[], _channels: string[], cursor?: string) => ({
      items: sources[0] === 's2' ? [projection('b', 's2')] : cursor ? [projection('b', 's2')] : [projection('a')],
      next_cursor: sources[0] === 's2' || cursor ? null : 'opaque+/cursor',
    }));
    renderPanel();
    await screen.findByText('Media a');
    await userEvent.click(screen.getByRole('tab', { name: 'Feed two' }));
    await screen.findByText('Media b');
    expect(mocks.list).toHaveBeenLastCalledWith(['s2'], [], undefined);
    await userEvent.click(screen.getByRole('tab', { name: 'All sources' }));
    await screen.findByText('Media a');
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(mocks.list).toHaveBeenLastCalledWith(['s1', 's2'], [], 'opaque+/cursor'));
  });

  it('searches loaded projection metadata and sends only immutable IDs to Ask AI', async () => {
    mocks.list.mockResolvedValue({ items: [projection('hero'), projection('a')], next_cursor: null });
    renderPanel(['s1']);
    await screen.findByText('Media a');
    fireEvent.change(screen.getByRole('textbox', { name: 'Search media' }), { target: { value: 'creator' } });
    expect(screen.getByText('Media a')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Ask AI' }));
    expect(mocks.ask).toHaveBeenCalledWith({
      context: { kind: 'selection', items: [{ sourceId: 's1', documentId: 'a', documentVersionId: 'a-v1' }] },
    });
    expect(JSON.stringify(mocks.ask.mock.calls)).not.toContain('Projection excerpt');
  });

  it('supports owner read and bookmark interactions', async () => {
    mocks.list.mockResolvedValue({ items: [projection('hero'), projection('a')], next_cursor: null });
    renderPanel(['s1']);
    await screen.findByText('Media a');
    await userEvent.click(screen.getByRole('button', { name: 'Mark read' }));
    expect(mocks.interact).toHaveBeenCalledWith('a', 1, { read: true, bookmarked: undefined }, 'csrf');
    expect(screen.getByRole('button', { name: 'Bookmark' })).toBeTruthy();
  });

  it('shows a load-more error alert when the next cursor page fails', async () => {
    mocks.list.mockImplementation(async (_sources: string[], _channels: string[], cursor?: string) => {
      if (cursor) throw new Error('boom');
      return { items: [projection('a')], next_cursor: 'c1' };
    });
    renderPanel(['s1']);
    await screen.findByText('Media a');
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(await screen.findByText('Could not load more items. Try again.')).toBeTruthy();
  });

  it('keeps loaded videos and shows only the load-more alert when a later page fails', async () => {
    mocks.list.mockImplementation(async (_sources: string[], _channels: string[], cursor?: string) => {
      if (cursor) throw new Error('boom');
      return { items: [projection('a')], next_cursor: 'c1' };
    });
    renderPanel(['s1']);
    await screen.findByText('Media a');
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(await screen.findByText('Could not load more items. Try again.')).toBeTruthy();
    expect(screen.queryByText('Could not load selected source records.')).toBeNull();
    expect(screen.getByText('Media a')).toBeTruthy();
  });

  it('shows the generic load failure when the first page fails', async () => {
    mocks.list.mockRejectedValue(new Error('boom'));
    renderPanel(['s1']);
    expect(await screen.findByText('Could not load selected source records.')).toBeTruthy();
    expect(screen.queryByText('Could not load more items. Try again.')).toBeNull();
  });

  it('no longer shows Translate all in the collection bar (it lives in the gadget menu)', async () => {
    mocks.list.mockResolvedValue({ items: [projection('a')], next_cursor: null });
    mocks.submit.mockResolvedValue({
      batch_id: null,
      items: [{ resource_type: 'document', resource_id: 'a', status: 'unchanged' }],
      settings: { enabled: true, target_language: 'vi', configuration_revision: 1 },
    });
    renderPanel(['s1']);
    await screen.findByText('Media a');
    expect(await screen.findByText('Media a')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Translate all' })).toBeNull();
    expect(mocks.submit).not.toHaveBeenCalled();
  });
});
