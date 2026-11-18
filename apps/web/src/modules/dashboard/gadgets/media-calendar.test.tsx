import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { calendarGroups, MediaCalendarGadget } from './media-calendar';
import { mediaInstance, mediaItem, renderMedia } from './media-test-support';
import { getCatalog } from '@locales';

const mocks = vi.hoisted(() => ({ list: vi.fn(), interaction: vi.fn(), openDrawer: vi.fn() }));
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
vi.mock('@/core/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/core/api')>()),
  apiRequest: vi.fn(async () => ({ privacy: { allow_widget_remote_media: true } })),
}));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isOwner: true }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ openDrawer: mocks.openDrawer }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/modules/sources/use-source-actions', () => ({ collectAndWait: vi.fn() }));

/** Creates a fictional release projection for deterministic timezone and ordering checks. */
function release(id: string, date: string | null) {
  const item = mediaItem(id);
  return { ...item, media: { ...item.media!, release_date: date } };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-11-17T10:00:00Z'));
  mocks.list.mockResolvedValue({ items: [release('today', '2026-11-17T12:00:00Z')], next_cursor: null });
  mocks.interaction.mockResolvedValue({});
});
afterEach(() => vi.restoreAllMocks());

describe('calendarGroups', () => {
  it('orders all buckets and sorts instants across explicit offsets, omitting unknown dates', () => {
    const items = [
      release('later', '2026-11-17T05:00:00-08:00'),
      release('early', '2026-11-17T12:00:00Z'),
      release('recent', '2026-11-16T12:00:00Z'),
      release('tomorrow', '2026-11-18T12:00:00Z'),
      release('week', '2026-11-22T12:00:00Z'),
      release('upcoming', '2026-11-23T12:00:00Z'),
      release('unknown', null),
      release('invalid', 'invalid'),
    ];
    expect(calendarGroups(items, 'UTC')).toMatchObject([
      { key: 'recent', items: [{ document_id: 'recent' }] },
      { key: 'today', items: [{ document_id: 'early' }, { document_id: 'later' }] },
      { key: 'tomorrow', items: [{ document_id: 'tomorrow' }] },
      { key: 'week', items: [{ document_id: 'week' }] },
      { key: 'upcoming', items: [{ document_id: 'upcoming' }] },
    ]);
  });

  it('reads date-only release dates as calendar days in any display timezone', () => {
    const now = Date.parse('2026-11-17T10:00:00Z');
    expect(calendarGroups([release('d', '2026-11-17')], 'America/Los_Angeles', now)).toMatchObject([{ key: 'today' }]);
  });

  it('uses display-timezone calendar days across DST and lets tomorrow precede the next week', () => {
    const now = Date.parse('2026-11-01T07:30:00Z');
    const items = [release('today', '2026-11-02T07:30:00Z'), release('tomorrow', '2026-11-02T08:30:00Z')];
    expect(calendarGroups(items, 'America/Los_Angeles', now)).toMatchObject([
      { key: 'today', items: [{ document_id: 'today' }] },
      { key: 'tomorrow', items: [{ document_id: 'tomorrow' }] },
    ]);
  });
});

describe('MediaCalendarGadget', () => {
  it('shows the empty notice when no record has a valid release date', async () => {
    mocks.list.mockResolvedValue({ items: [release('nodate', null)], next_cursor: null });
    renderMedia(<MediaCalendarGadget instance={mediaInstance({ renderer: 'media_calendar' })} />);
    expect(await screen.findByText('No media matches these filters.')).toBeInTheDocument();
  });

  it('uses effective scope/filter selectors and the ascending release cursor contract', async () => {
    mocks.list.mockImplementation(async (_sources, _channels, cursor) => ({
      items: [release(cursor ? 'next' : 'today', cursor ? '2026-11-18T12:00:00Z' : '2026-11-17T12:00:00Z')],
      next_cursor: cursor ? null : 'release+cursor/=',
    }));
    renderMedia(
      <MediaCalendarGadget
        instance={mediaInstance({
          renderer: 'media_calendar',
          scope: {
            release_window_days: 14,
            countries: ['JP'],
            genres: ['comedy'],
            languages: ['ja'],
            formats: ['tv'],
            content_ratings: ['safe'],
            release_year: 2026,
            release_season: 'fall',
            min_rating: 80,
          },
          filters: { genres: ['adventure'], media_type: 'anime', release_status: 'airing' },
        })}
      />,
    );
    await screen.findByText('Title today');
    expect(mocks.list).toHaveBeenCalledWith(
      ['s1', 's2'],
      [],
      undefined,
      expect.objectContaining({
        sort: 'release_date_asc',
        release_window_days: 14,
        countries: ['JP'],
        genres: ['adventure'],
        languages: ['ja'],
        formats: ['tv'],
        content_ratings: ['safe'],
        release_year: 2026,
        release_season: 'fall',
        min_rating: 80,
        media_types: ['anime'],
        release_statuses: ['airing'],
      }),
    );
    expect(screen.getByRole('heading', { name: 'Today' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByRole('heading', { name: 'Tomorrow' });
    expect(mocks.list).toHaveBeenLastCalledWith(
      ['s1', 's2'],
      [],
      'release+cursor/=',
      expect.objectContaining({ sort: 'release_date_asc' }),
    );
  });

  it('restarts the server query on window and source changes and sends local-only identities to chat', async () => {
    mocks.list.mockResolvedValue({ items: [{ ...release('local', '2026-11-17T12:00:00Z'), source_id: 's2' }], next_cursor: null });
    renderMedia(<MediaCalendarGadget instance={mediaInstance({ renderer: 'media_calendar' })} />);
    await screen.findByText('Title local');
    await userEvent.click(screen.getByRole('combobox', { name: 'Release window (days)' }));
    await userEvent.click(screen.getByRole('option', { name: '7 days' }));
    await waitFor(() =>
      expect(mocks.list).toHaveBeenLastCalledWith(['s1', 's2'], [], undefined, expect.objectContaining({ release_window_days: 7 })),
    );
    await userEvent.click(await screen.findByRole('tab', { name: 'Second' }));
    await waitFor(() =>
      expect(mocks.list).toHaveBeenLastCalledWith(
        ['s2'],
        [],
        undefined,
        expect.objectContaining({ sort: 'release_date_asc', release_window_days: 7 }),
      ),
    );
    await userEvent.click(await screen.findByRole('button', { name: 'Ask AI' }));
    expect(mocks.openDrawer).toHaveBeenCalledWith({
      context: { kind: 'selection', items: [{ sourceId: 's2', documentId: 'local', documentVersionId: 'local-v1' }] },
    });
  });

  it('localizes calendar buckets and exposes shared read/bookmark/dismiss actions', async () => {
    renderMedia(<MediaCalendarGadget instance={mediaInstance({ renderer: 'media_calendar' })} />, true, 'vi-vi');
    expect(await screen.findByRole('heading', { name: 'Hôm nay' })).toBeInTheDocument();
    expect(screen.getByText('Fiction provider')).toBeInTheDocument();
    const labels = getCatalog('vi-vi', 'dashboard');
    await userEvent.click(screen.getByRole('button', { name: labels.markRead }));
    await waitFor(() => expect(mocks.interaction).toHaveBeenCalledWith('today', 1, expect.objectContaining({ read: true }), 'csrf'));
    await userEvent.click(screen.getByRole('button', { name: labels.bookmark }));
    await waitFor(() => expect(mocks.interaction).toHaveBeenCalledWith('today', 1, expect.objectContaining({ bookmarked: true }), 'csrf'));
    await userEvent.click(screen.getByRole('button', { name: labels.feed.notRelevant }));
    expect(screen.queryByText('Title today')).toBeNull();
  });
});
