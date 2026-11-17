import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MediaFeedGadget } from './media-feed';
import { mediaDefaults } from './media-parts';
import { mediaInstance, mediaItem, renderMedia } from './media-test-support';
import { GadgetFrame } from '../gadget-frame';
import { FeedImage } from '@/components/feed-image';

const mocks = vi.hoisted(() => ({ list: vi.fn(), interaction: vi.fn(), openDrawer: vi.fn(), owner: true, privacy: vi.fn() }));
vi.mock('@/modules/knowledge/public-api', () => ({ documentKeys: { all: ['documents'] }, listGadgetDocumentProjections: mocks.list }));
vi.mock('@/modules/knowledge', () => ({ documentKeys: { all: ['documents'] }, setGadgetDocumentInteraction: mocks.interaction }));
vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  listGadgetSources: vi.fn(async () => ({
    items: [
      { id: 's1', name: 'First' },
      { id: 's2', name: 'Second', local_only: true },
    ],
  })),
}));
vi.mock('@/core/api', async (importOriginal) => ({ ...(await importOriginal<typeof import('@/core/api')>()), apiRequest: mocks.privacy }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isOwner: mocks.owner }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ openDrawer: mocks.openDrawer }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/modules/sources/use-source-actions', () => ({ collectAndWait: vi.fn() }));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.owner = true;
  mocks.list.mockResolvedValue({ items: [mediaItem()], next_cursor: null });
  mocks.interaction.mockResolvedValue({});
  mocks.privacy.mockResolvedValue({ privacy: { allow_widget_remote_media: true } });
});

describe('MediaFeedGadget', () => {
  it('preserves legacy FeedImage defaults, missing-image collapse and failure behavior', () => {
    const { container, rerender } = renderMedia(<FeedImage src="/image" className="mb-2" />);
    expect(container.firstChild).toHaveClass('aspect-video', 'w-full', 'overflow-hidden', 'rounded-md', 'bg-muted', 'mb-2');
    fireEvent.error(container.querySelector('img')!);
    expect(container).toBeEmptyDOMElement();
    rerender(<FeedImage src={null} />);
    expect(container).toBeEmptyDOMElement();
  });
  it('applies populated filter overrides, scope fallbacks and zero rating', async () => {
    const instance = mediaInstance({
      scope: { countries: ['JP'], genres: ['comedy'], min_rating: 90, languages: ['ja'] },
      filters: { countries: [], genres: ['adventure'], min_rating: 0 },
    });
    expect(mediaDefaults(instance.definition)).toMatchObject({
      countries: ['JP'],
      genres: ['adventure'],
      languages: ['ja'],
      min_rating: 0,
    });
    renderMedia(<MediaFeedGadget instance={instance} />);
    await screen.findByText('Title doc-1');
    expect(mocks.list).toHaveBeenCalledWith(
      ['s1', 's2'],
      [],
      undefined,
      expect.objectContaining({ countries: ['JP'], genres: ['adventure'], languages: ['ja'], min_rating: 0, sort: 'created_at_desc' }),
    );
  });

  it('uses the projected aspect, attribution and safe links; failures remove only artwork', async () => {
    const poster = mediaItem();
    const square = mediaItem('music', { media: { ...poster.media!, media_type: 'music', artwork_aspect: 'square' } });
    const video = mediaItem('video', {
      media: { ...poster.media!, media_type: 'youtube', artwork_aspect: 'video' },
      canonical_url: 'javascript:alert(1)',
    });
    mocks.list.mockResolvedValue({ items: [poster, square, video], next_cursor: null });
    const { container } = renderMedia(<MediaFeedGadget instance={mediaInstance()} />);
    await screen.findByText('Title doc-1');
    expect(container.querySelector('.aspect-\\[2\\/3\\]')).toBeTruthy();
    expect(container.querySelector('.aspect-square')).toBeTruthy();
    expect(container.querySelector('.aspect-video')).toBeTruthy();
    expect(screen.getAllByText('Fiction provider')).toHaveLength(3);
    expect(screen.getByText('Title video').closest('a')).toBeNull();
    fireEvent.error(container.querySelector('img')!);
    expect(container.querySelectorAll('img')).toHaveLength(2);
    await userEvent.click(screen.getByRole('button', { name: 'List' }));
    expect(localStorage.getItem('umwelt.dashboard.mediaView.media-1')).toBe('list');
  });

  it('notices explicit privacy false, while missing artwork with privacy true has no notice', async () => {
    mocks.list.mockResolvedValue({ items: [mediaItem('no-art', { image_url: null })], next_cursor: null });
    const { client } = renderMedia(<MediaFeedGadget instance={mediaInstance()} />);
    await screen.findByText('Title no-art');
    expect(screen.queryByText(/Cover art is disabled/)).toBeNull();
    client.setQueryData(['ai-settings'], { privacy: { allow_widget_remote_media: false } });
    expect(await screen.findByRole('link', { name: /Cover art is disabled/ })).toHaveAttribute('href', '/settings/models#ai-widget-media');
  });

  it('passes the opaque cursor, deduplicates overlapping pages and restarts paging on source selection', async () => {
    mocks.list.mockImplementation(async (_sources, _channels, cursor) =>
      cursor ? { items: [mediaItem(), mediaItem('next')], next_cursor: null } : { items: [mediaItem()], next_cursor: 'opaque+cursor/=' },
    );
    renderMedia(<MediaFeedGadget instance={mediaInstance()} />);
    await screen.findByText('Title doc-1');
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByText('Title next');
    expect(mocks.list).toHaveBeenCalledWith(['s1', 's2'], [], 'opaque+cursor/=', expect.anything());
    expect(screen.getAllByText('Title doc-1')).toHaveLength(1);
    await userEvent.click(await screen.findByRole('tab', { name: 'Second' }));
    await waitFor(() => expect(mocks.list).toHaveBeenLastCalledWith(['s2'], [], undefined, expect.anything()));
  });

  it('removes inherited filter chips, clears filters, resets defaults and supports valid year typing', async () => {
    renderMedia(<MediaFeedGadget instance={mediaInstance({ scope: { countries: ['JP'], genres: ['comedy'], min_rating: 0 } })} />);
    await screen.findByText('Title doc-1');
    await userEvent.click(screen.getByRole('button', { name: 'Remove filter: JP' }));
    await waitFor(() =>
      expect(mocks.list).toHaveBeenLastCalledWith(expect.anything(), [], undefined, expect.objectContaining({ countries: [] })),
    );
    await userEvent.click(screen.getByRole('button', { name: 'Filter (2)' }));
    const year = screen.getByRole('spinbutton', { name: 'Release year' });
    await userEvent.type(year, '2026');
    await userEvent.tab();
    await waitFor(() =>
      expect(mocks.list).toHaveBeenLastCalledWith(expect.anything(), [], undefined, expect.objectContaining({ release_year: 2026 })),
    );
    const savedYear = screen.getByRole('spinbutton', { name: 'Release year' });
    await userEvent.clear(savedYear);
    await userEvent.type(savedYear, '2027');
    expect(savedYear).toHaveFocus();
    await userEvent.tab();
    await waitFor(() =>
      expect(mocks.list).toHaveBeenLastCalledWith(expect.anything(), [], undefined, expect.objectContaining({ release_year: 2027 })),
    );
    await userEvent.click(screen.getByRole('button', { name: 'Reset to defaults' }));
    expect(await screen.findByRole('button', { name: 'Remove filter: JP' })).toBeInTheDocument();
    expect(screen.getByRole('spinbutton', { name: 'Release year' })).toHaveValue(null);
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clear all' }));
    await waitFor(() =>
      expect(mocks.list).toHaveBeenLastCalledWith(expect.anything(), [], undefined, {
        release_statuses: undefined,
        sort: 'created_at_desc',
      }),
    );
  });

  it('keeps scope media types independent from the quick type selector', async () => {
    renderMedia(
      <MediaFeedGadget instance={mediaInstance({ scope: { media_types: ['anime', 'movie'] }, filters: { media_type: 'movie' } })} />,
    );
    await screen.findByText('Title doc-1');
    expect(mocks.list).toHaveBeenCalledWith(expect.anything(), [], undefined, expect.objectContaining({ media_types: ['movie'] }));
    await userEvent.click(screen.getAllByRole('button', { name: 'Remove filter: Movie' }).at(-1)!);
    await waitFor(() =>
      expect(mocks.list).toHaveBeenLastCalledWith(
        expect.anything(),
        [],
        undefined,
        expect.objectContaining({ media_types: ['anime', 'movie'] }),
      ),
    );
  });

  it('asks with identity only and persists read/bookmark/dismiss with Undo', async () => {
    renderMedia(<MediaFeedGadget instance={mediaInstance()} />);
    await screen.findByText('Title doc-1');
    await userEvent.click(screen.getByRole('button', { name: 'Ask AI' }));
    expect(mocks.openDrawer).toHaveBeenCalledWith({
      context: { kind: 'selection', items: [{ sourceId: 's1', documentId: 'doc-1', documentVersionId: 'doc-1-v1' }] },
    });
    await userEvent.click(screen.getByRole('button', { name: 'Mark read' }));
    await waitFor(() => expect(mocks.interaction).toHaveBeenCalledWith('doc-1', 1, expect.objectContaining({ read: true }), 'csrf'));
    await userEvent.click(screen.getByRole('button', { name: 'Bookmark' }));
    await waitFor(() => expect(mocks.interaction).toHaveBeenCalledWith('doc-1', 1, expect.objectContaining({ bookmarked: true }), 'csrf'));
    await userEvent.click(screen.getByRole('button', { name: 'Not relevant' }));
    expect(screen.queryByText('Title doc-1')).toBeNull();
    await userEvent.click(await screen.findByRole('button', { name: 'Undo' }));
    await screen.findByText('Title doc-1');
    await waitFor(() =>
      expect(mocks.interaction).toHaveBeenLastCalledWith('doc-1', 1, expect.objectContaining({ dismissed: false }), 'csrf'),
    );
  });

  it('keeps selections bounded and excludes hidden rows from bulk Ask AI', async () => {
    mocks.list.mockResolvedValue({ items: Array.from({ length: 34 }, (_, i) => mediaItem(String(i))), next_cursor: null });
    renderMedia(<MediaFeedGadget instance={mediaInstance()} />);
    await screen.findByText('Title 33');
    for (const checkbox of screen.getAllByRole('checkbox')) fireEvent.click(checkbox);
    const bulk = screen.getByRole('button', { name: 'Ask AI about selection' });
    await userEvent.click(bulk);
    expect(mocks.openDrawer.mock.lastCall![0].context.items).toHaveLength(32);
    await userEvent.click(within(screen.getByText('Title 0').closest('article')!).getByRole('button', { name: 'Not relevant' }));
    await userEvent.click(screen.getByRole('button', { name: 'Ask AI about selection' }));
    expect(mocks.openDrawer.mock.lastCall![0].context.items).toHaveLength(31);
    expect(mocks.openDrawer.mock.lastCall![0].context.items).not.toContainEqual(expect.objectContaining({ documentId: '0' }));
  });

  it('hides chat and write actions from read-only workspace viewers', async () => {
    mocks.owner = false;
    renderMedia(<MediaFeedGadget instance={mediaInstance()} />);
    await screen.findByText('Title doc-1');
    expect(screen.queryByRole('button', { name: 'Ask AI' })).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Bookmark' })).toBeNull();
    expect(mocks.privacy).not.toHaveBeenCalled();
  });

  it('keeps active filters and the same card DOM through frame expansion and collapse', async () => {
    const instance = mediaInstance({ scope: { countries: ['JP'] } });
    renderMedia(
      <GadgetFrame instance={instance} isEditMode={false}>
        <MediaFeedGadget instance={instance} />
      </GadgetFrame>,
    );
    await screen.findByText('Title doc-1');
    await userEvent.click(screen.getByRole('button', { name: 'Remove filter: JP' }));
    const title = await screen.findByText('Title doc-1');
    await userEvent.click(screen.getByRole('button', { name: 'Expand gadget' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toContainElement(title));
    expect(screen.queryByRole('button', { name: 'Remove filter: JP' })).toBeNull();
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByText('Title doc-1')).toBe(title);
  });

  it('groups multiple sources and keeps search/unread filters local to loaded pages', async () => {
    mocks.list.mockResolvedValue({
      items: [mediaItem(), mediaItem('second', { source_id: 's2', read_at: '2026-11-17T01:00:00Z' })],
      next_cursor: null,
    });
    renderMedia(<MediaFeedGadget instance={mediaInstance({ filters: { group_by: 'source' } })} />);
    expect(await screen.findByRole('heading', { name: 'First' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Second' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Unread only' }));
    expect(screen.queryByText('Title second')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Filter' }));
    await userEvent.type(screen.getByRole('searchbox'), 'missing title');
    expect(await screen.findByText('No media matches these filters.')).toBeInTheDocument();
    expect(mocks.list).toHaveBeenCalledTimes(1);
  });

  it('does not widen conflicting media selectors or query with no selected sources', async () => {
    const { unmount } = renderMedia(
      <MediaFeedGadget instance={mediaInstance({ scope: { media_types: ['anime'] }, filters: { media_type: 'movie' } })} />,
    );
    expect(await screen.findByText('No media matches these filters.')).toBeInTheDocument();
    expect(mocks.list).not.toHaveBeenCalled();
    unmount();
    renderMedia(<MediaFeedGadget instance={mediaInstance({ source_ids: [] })} />);
    expect(await screen.findByText('Choose media or video sources to show releases.')).toBeInTheDocument();
    expect(mocks.list).not.toHaveBeenCalled();
  });

  it('uses the privacy query for unknown settings and surfaces mutation errors', async () => {
    mocks.privacy.mockResolvedValue({ privacy: { allow_widget_remote_media: false } });
    mocks.interaction.mockRejectedValue(new Error('save failed'));
    renderMedia(<MediaFeedGadget instance={mediaInstance()} />, undefined);
    await screen.findByText('Title doc-1');
    expect(mocks.privacy).toHaveBeenCalledWith('/api/v1/settings/ai');
    expect(await screen.findByRole('link', { name: /Cover art is disabled/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Bookmark' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save this item action. Try again.');
  });
});
