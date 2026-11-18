import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';
import type { ConnectorCatalogEntry, ConnectorConfig } from './api';
import { ApiKeyField } from './connector-connect-sections';
import { defaultConfiguration } from './connector-editor-model';
import { ScheduleStep } from './connector-editor/schedule-step';
import type { ConnectorEditorModel } from './connector-editor-types';
import type { BuiltinSource } from './api';
import { ProviderScope, type NativeProvider } from './provider-scope';
import { scopeGate as productionScopeGate } from './use-connector-wizard';
import { MEDIA_PROVIDER_FIELDS, MEDIA_FILTER_SPECS, OPTION_SPECS } from './builtin-option-specs';
import { OPTION_LABEL_KEY, optionValueKey } from './builtin-option-keys';
import { ANIMESEASON, MEDIAGENRE, MEDIALANGUAGE, MEDIAFORMAT, CONTENTRATING } from '@/core/media-options';

/** Supplies the normal shared connector defaults to focused scope fixtures. */
function config(patch: Partial<ConnectorConfig>): ConnectorConfig {
  return { timeout_seconds: 30, timezone: 'UTC', schedule_interval_minutes: 60, ...patch };
}
/** Evaluates the production wizard gate with a focused scope fixture. */
function scopeGate(provider: string, patch: Partial<ConnectorConfig>) {
  return productionScopeGate(provider, config(patch));
}
/** Render scope controls against the real bilingual catalogs and a controlled draft. */
function view(provider: NativeProvider, configuration: Partial<ConnectorConfig> = {}, locale: 'en-us' | 'vi-vi' = 'en-us') {
  const onChange = vi.fn();
  const tree = (next: Partial<ConnectorConfig>, resetEpoch = 0) => (
    <NextIntlClientProvider locale={locale === 'en-us' ? 'en-US' : 'vi-VN'} messages={{ sources: getCatalog(locale, 'sources') }}>
      <ProviderScope provider={provider} configuration={config(next)} disabled={false} resetEpoch={resetEpoch} onChange={onChange} />
    </NextIntlClientProvider>
  );
  const result = render(tree(configuration));
  return {
    onChange,
    rerender: (next: Partial<ConnectorConfig>, epoch = 0) => result.rerender(tree(next, epoch)),
    unmount: result.unmount,
  };
}

afterEach(cleanup);

describe('media source scope', () => {
  it.each(Object.keys(MEDIA_PROVIDER_FIELDS) as (keyof typeof MEDIA_PROVIDER_FIELDS)[])(
    'renders %s without required optional filters',
    (provider) => {
      const { container, unmount } = render(
        <NextIntlClientProvider locale="en-US" messages={{ sources: getCatalog('en-us', 'sources') }}>
          <ProviderScope
            provider={provider}
            configuration={config(provider === 'podcast_rss' ? { itunes_podcast_id: 1 } : {})}
            disabled={false}
            resetEpoch={0}
            onChange={vi.fn()}
          />
        </NextIntlClientProvider>,
      );
      const required = [...container.querySelectorAll('[required]')].map((node) => node.id);
      expect(required).toEqual(provider === 'youtube_api' || provider === 'youtube' ? [`source-scope-youtube_channel_id`] : []);
      expect(screen.queryByLabelText('Telegram channel IDs')).not.toBeInTheDocument();
      unmount();
    },
  );

  it('uses the same canonical enum values and bounded specs as the media editor', () => {
    const spec = (key: string) => MEDIA_FILTER_SPECS.find((item) => item.key === key)!;
    expect(spec('media_genres')).toMatchObject({ values: MEDIAGENRE, max: 16 });
    expect(spec('media_languages')).toMatchObject({ values: MEDIALANGUAGE, max: 16 });
    expect(spec('media_formats')).toMatchObject({ values: MEDIAFORMAT, max: 16 });
    expect(spec('content_ratings')).toMatchObject({ values: CONTENTRATING, max: 8 });
    expect(spec('anime_season').values).toEqual(ANIMESEASON);
    expect(spec('min_rating')).toMatchObject({ min: 0, hi: 100 });
    expect(spec('release_year')).toMatchObject({ min: 1800, hi: 2100 });
    for (const item of OPTION_SPECS) {
      expect(OPTION_LABEL_KEY).toHaveProperty(item.key);
      if (item.labelled) for (const value of item.values ?? []) expect(optionValueKey(value)).toBeTruthy();
    }
  });

  it.each([
    ['tmdb_movies', ['Now playing', 'Upcoming', 'Changed movies'], ['Airing today', 'On the air']],
    ['tmdb_tv', ['Airing today', 'On the air'], ['Now playing', 'Upcoming', 'Changed movies']],
  ] as const)('limits %s TMDB modes to the provider modes accepted by validate_options', async (provider, allowed, rejected) => {
    const user = userEvent.setup();
    const { unmount } = view(provider);

    await user.click(screen.getByLabelText('TMDB list'));
    for (const label of allowed) expect(screen.getByRole('option', { name: label })).toBeInTheDocument();
    for (const label of rejected) expect(screen.queryByRole('option', { name: label })).not.toBeInTheDocument();
    unmount();
  });

  it('clears and hides TMDB metadata filters for the ID-only changes mode', async () => {
    const user = userEvent.setup();
    const { onChange, rerender } = view('tmdb_movies', {
      tmdb_mode: 'upcoming',
      tmdb_certification: 'PG-13',
      tmdb_certification_country: 'US',
      min_rating: 80,
    });
    expect(screen.getByLabelText('Maximum certification')).toBeInTheDocument();
    expect(screen.getByLabelText('Certification country')).toBeInTheDocument();

    await user.click(screen.getByLabelText('TMDB list'));
    await user.click(screen.getByRole('option', { name: 'Changed movies' }));
    expect(onChange).toHaveBeenCalledWith('tmdb_certification', null);
    expect(onChange).toHaveBeenCalledWith('tmdb_certification_country', null);
    expect(onChange).toHaveBeenCalledWith('min_rating', null);

    rerender({ tmdb_mode: 'changes' });
    expect(screen.queryByLabelText('Maximum certification')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Certification country')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Minimum rating (0–100)')).not.toBeInTheDocument();
  });

  it('clears OMDb detail-only filters when switching to search while keeping the year filter', async () => {
    const user = userEvent.setup();
    const { onChange, rerender } = view('omdb_lookup', {
      omdb_mode: 'title',
      omdb_title: 'Dune',
      min_rating: 80,
      media_genres: ['sci_fi'],
    });
    expect(screen.getByLabelText('Minimum rating (0–100)')).toBeInTheDocument();
    await user.click(screen.getByLabelText('Lookup by'));
    await user.click(screen.getByRole('option', { name: 'Search' }));
    for (const field of ['media_country', 'media_countries', 'media_genres', 'media_languages', 'min_rating']) {
      expect(onChange).toHaveBeenCalledWith(field, null);
    }

    rerender({ omdb_mode: 'search' });
    expect(screen.getByLabelText('Search title')).toBeInTheDocument();
    expect(screen.getByLabelText('Release year')).toBeInTheDocument();
    expect(screen.queryByText('Genres')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Minimum rating (0–100)')).not.toBeInTheDocument();
  });

  it('changes MAL anime from ranking to the seasonal endpoint and requires both season fields', async () => {
    const user = userEvent.setup();
    const { onChange, rerender } = view('mal_anime', { mal_ranking_type: 'airing' });
    expect(screen.getByLabelText('Ranking type')).toBeInTheDocument();
    expect(screen.queryByLabelText('Season year')).not.toBeInTheDocument();

    await user.click(screen.getByLabelText('MyAnimeList list'));
    await user.click(screen.getByRole('option', { name: 'Seasonal anime' }));
    expect(onChange).toHaveBeenCalledWith('mal_mode', 'season');
    expect(onChange).toHaveBeenCalledWith('mal_ranking_type', null);

    rerender({ mal_mode: 'season' });
    expect(screen.queryByLabelText('Ranking type')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Season year')).toBeInTheDocument();
    expect(scopeGate('mal_anime', { mal_mode: 'season', mal_season: 'fall' })).toBe(false);
    expect(scopeGate('mal_anime', { mal_mode: 'season', mal_season: 'fall', mal_season_year: 2026 })).toBe(true);
  });

  it('offers only manga rankings in MyAnimeList manga', async () => {
    const user = userEvent.setup();
    view('mal_manga');
    await user.click(screen.getByLabelText('Ranking type'));
    expect(screen.getByRole('option', { name: 'Manhwa' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /^All$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Airing' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('MyAnimeList list')).not.toBeInTheDocument();
  });

  it('localizes Google Books targets and accepts a query or subject before optional filters', () => {
    view('google_books', {}, 'vi-vi');
    expect(screen.getByLabelText('Truy vấn sách')).toBeInTheDocument();
    expect(screen.getByLabelText('Chủ đề sách')).toBeInTheDocument();
    expect(scopeGate('google_books', { min_rating: 80 })).toBe(false);
    expect(scopeGate('google_books', { google_books_query: 'intitle:Dune' })).toBe(true);
    expect(scopeGate('google_books', { google_books_subject: 'Lịch sử' })).toBe(true);
  });

  it('requires the owner to choose the Google Books target even with a catalog example', () => {
    const entry = {
      kind: 'preset',
      example_config: config({ google_books_subject: 'History', schedule_interval_minutes: 1440 }),
    } as unknown as ConnectorCatalogEntry;
    const draft = defaultConfiguration('google_books', entry);
    expect(draft.schedule_interval_minutes).toBe(1440);
    expect(productionScopeGate('google_books', draft)).toBe(false);
  });

  it.each([
    ['mal_anime', 'MyAnimeList client ID'],
    ['mal_manga', 'MyAnimeList client ID'],
    ['google_books', 'Google Books API key'],
    ['hardcover_books', 'Hardcover API token'],
    ['youtube_api', 'YouTube Data API key'],
    ['podcast_index', 'Podcast Index API key'],
  ])('labels the write-only credential for %s', (provider, label) => {
    const model = {
      provider,
      currentEntry: { key_fields: ['api_key'] },
      secret: '',
      providerCredentialConfigured: false,
    } as unknown as ConnectorEditorModel;
    render(
      <NextIntlClientProvider locale="en-US" messages={{ sources: getCatalog('en-us', 'sources') }}>
        <ApiKeyField m={model} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByLabelText(label)).toBeRequired();
    expect(screen.getByLabelText(label)).toHaveAttribute('type', 'password');
    if (provider === 'hardcover_books') expect(screen.getByLabelText(label)).toHaveAttribute('maxlength', '512');
    if (provider === 'podcast_index') {
      expect(screen.getByLabelText('Podcast Index API secret')).toBeRequired();
      expect(screen.getByLabelText('Podcast Index API secret')).toHaveAttribute('type', 'password');
    }
  });

  it('localizes the Hardcover scope and keeps collection filters optional', () => {
    view('hardcover_books', { hardcover_mode: 'catalog' }, 'vi-vi');
    expect(screen.getByLabelText('Phạm vi thu thập Hardcover')).toHaveTextContent('Danh mục sách');
    expect(scopeGate('hardcover_books', { min_rating: 80 })).toBe(false);
    expect(scopeGate('hardcover_books', { hardcover_mode: 'upcoming' })).toBe(true);
  });

  it('uses the YouTube target families and only allows All videos for playlist targets', () => {
    view('youtube_api', { youtube_playlist_ids: ['PL0123456789'] });
    expect(screen.getByLabelText('Video format')).toBeDisabled();
    expect(screen.queryByText('Countries')).not.toBeInTheDocument();
    expect(scopeGate('youtube_api', { youtube_handles: ['@example'] })).toBe(true);
    expect(scopeGate('youtube_api', { youtube_playlist_ids: ['PL0123456789'], youtube_filter: 'shorts' })).toBe(false);
  });

  it('clears YouTube channel country and format when switching to a direct playlist', async () => {
    const user = userEvent.setup();
    const { onChange } = view('youtube_api', { youtube_handles: ['@example'], youtube_filter: 'shorts', media_countries: ['VN'] });
    await user.click(screen.getByLabelText('YouTube targets'));
    await user.click(screen.getByRole('option', { name: 'Playlist IDs' }));
    expect(onChange).toHaveBeenCalledWith('youtube_filter', 'all');
    expect(onChange).toHaveBeenCalledWith('media_countries', null);
  });

  it('offers a YouTube most-popular mode without targets and an optional region and category', async () => {
    const user = userEvent.setup();
    const { onChange, unmount } = view('youtube_api', { youtube_channel_id: 'UCabcdefghijABCDEFGHIJ12' });
    await user.click(screen.getByLabelText('Source'));
    await user.click(screen.getByRole('option', { name: 'Most popular videos' }));
    expect(onChange).toHaveBeenCalledWith('youtube_api_mode', 'most_popular');
    expect(onChange).toHaveBeenCalledWith('youtube_channel_id', null);
    expect(onChange).toHaveBeenCalledWith('youtube_playlist_ids', null);
    unmount();
    const popular = view('youtube_api', { youtube_api_mode: 'most_popular' }, 'vi-vi');
    expect(screen.queryByLabelText('YouTube targets')).toBeNull();
    expect(screen.getByLabelText('ID danh mục video (tùy chọn)')).not.toBeRequired();
    fireEvent.change(screen.getByLabelText('ID danh mục video (tùy chọn)'), { target: { value: '10' } });
    expect(popular.onChange).toHaveBeenCalledWith('youtube_video_category_id', '10');
    expect(scopeGate('youtube_api', { youtube_api_mode: 'most_popular' })).toBe(true);
    expect(scopeGate('youtube_api', { youtube_api_mode: 'uploads' })).toBe(false);
  });

  it('clears region and category when returning to YouTube uploads', async () => {
    const user = userEvent.setup();
    const { onChange } = view('youtube_api', { youtube_api_mode: 'most_popular', media_country: 'US', youtube_video_category_id: '10' });
    await user.click(screen.getByLabelText('Source'));
    await user.click(screen.getByRole('option', { name: 'Channel uploads' }));
    expect(onChange).toHaveBeenCalledWith('youtube_api_mode', null);
    expect(onChange).toHaveBeenCalledWith('youtube_video_category_id', null);
    expect(onChange).toHaveBeenCalledWith('media_country', null);
  });

  it('shows the minimum rating filter for the YouTube feed', () => {
    view('youtube', { youtube_channel_id: 'UCabcdefghijABCDEFGHIJ12' });
    expect(screen.getByLabelText('Minimum rating (0–100)')).toBeInTheDocument();
  });

  it('shows Podcast Index scope fields for the selected endpoint', () => {
    const { rerender, onChange } = view('podcast_index');
    expect(screen.getByLabelText('Recent feeds since (Unix seconds)')).toBeInTheDocument();
    expect(screen.queryByLabelText('Value block type')).not.toBeInTheDocument();
    expect(screen.queryByText('Content ratings')).not.toBeInTheDocument();
    rerender({ podcast_index_query: 'History', content_ratings: ['explicit'], podcast_index_value: 'any' });
    expect(screen.getByLabelText('Value block type')).toBeInTheDocument();
    expect(screen.getByText('Content ratings')).toBeInTheDocument();
    expect(screen.queryByLabelText('Recent feeds since (Unix seconds)')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Podcast search term'), { target: { value: '' } });
    expect(onChange).toHaveBeenCalledWith('podcast_index_value', null);
    expect(onChange).toHaveBeenCalledWith('content_ratings', null);
  });

  it('requires Steam news app IDs and a bounded IGDB game scope', () => {
    view('steam_releases');
    expect(screen.getByLabelText('Steam collection mode')).toBeInTheDocument();
    expect(scopeGate('steam_releases', { steam_mode: 'news' })).toBe(false);
    expect(scopeGate('steam_releases', { steam_mode: 'news', steam_appids: [570] })).toBe(true);
    expect(scopeGate('igdb_games', {})).toBe(false);
    expect(scopeGate('igdb_games', { igdb_region: 'world' })).toBe(true);
  });

  it('allows empty optional filters but requires identifying source targets', () => {
    for (const provider of [
      'anilist_anime',
      'anilist_manga',
      'tmdb_movies',
      'tmdb_tv',
      'trakt_media',
      'mal_anime',
      'mal_manga',
      'gutendex_books',
      'mangadex_releases',
      'mangaupdates_releases',
      'ann_news',
      'livechart_episodes',
      'wikidata_movies',
      'tvmaze_schedule',
    ]) {
      expect(scopeGate(provider, {})).toBe(true);
    }
    expect(scopeGate('apple_books_rss', { country: 'US', kind: 'books', chart: 'top-paid' })).toBe(true);
    expect(scopeGate('apple_books_rss', { country: 'US' })).toBe(false);
    expect(scopeGate('itunes_movies', { search_terms: 'Dune', country: 'US' })).toBe(true);
    expect(scopeGate('itunes_movies', { country: 'US' })).toBe(false);
    expect(scopeGate('letterboxd_rss', { letterboxd_username: 'public-diary' })).toBe(true);
    expect(scopeGate('letterboxd_rss', {})).toBe(false);
    expect(scopeGate('podcast_rss', { feed_url: 'https://example.org/feed.xml' })).toBe(true);
    expect(scopeGate('podcast_rss', { itunes_podcast_id: 123 })).toBe(true);
    expect(scopeGate('podcast_rss', {})).toBe(false);
    expect(scopeGate('podcast_rss', { itunes_podcast_id: 123, feed_url: 'https://example.org/rss' })).toBe(false);
    expect(scopeGate('omdb_lookup', {})).toBe(false);
    expect(scopeGate('omdb_lookup', { omdb_mode: 'title', omdb_title: 'Dune' })).toBe(true);
    expect(scopeGate('omdb_lookup', { omdb_mode: 'search', omdb_search: 'Dune' })).toBe(true);
    expect(scopeGate('omdb_lookup', { omdb_mode: 'imdb_id', omdb_imdb_id: 'tt1234567' })).toBe(true);
    expect(scopeGate('openlibrary_books', {})).toBe(false);
    expect(scopeGate('openlibrary_books', { openlibrary_subject: 'science_fiction' })).toBe(true);
    expect(scopeGate('openlibrary_books', { openlibrary_mode: 'search', openlibrary_query: 'Dune' })).toBe(true);
    expect(scopeGate('openlibrary_books', { openlibrary_mode: 'search' })).toBe(false);
    expect(scopeGate('openlibrary_books', { openlibrary_mode: 'recent' })).toBe(true);
  });

  it('serializes numeric IDs and clears optional filters explicitly', () => {
    const { onChange } = view('anilist_anime', { media_genres: ['action'] });
    fireEvent.change(screen.getByLabelText('AniList media IDs'), { target: { value: '1, 2' } });
    expect(onChange).toHaveBeenCalledWith('media_ids', [1, 2]);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Action' }));
    expect(onChange).toHaveBeenCalledWith('media_genres', null);
  });

  it('keeps one Open Library language and clears incompatible fields when modes change', async () => {
    const user = userEvent.setup();
    const { onChange } = view('openlibrary_books', { openlibrary_mode: 'search', media_languages: ['en'] });
    expect(screen.getByRole('checkbox', { name: 'Vietnamese' })).toBeDisabled();
    await user.click(screen.getByLabelText('Open Library collection'));
    await user.click(screen.getByRole('option', { name: 'Recent changes' }));
    expect(onChange).toHaveBeenCalledWith('openlibrary_mode', 'recent');
    expect(onChange).toHaveBeenCalledWith('openlibrary_query', null);
    expect(onChange).toHaveBeenCalledWith('media_languages', null);
  });

  it('renders localized enum labels and retains explicit false for optional boolean choices', async () => {
    const user = userEvent.setup();
    const { onChange } = view('anilist_anime', { anime_season: 'spring' }, 'vi-vi');
    expect(screen.getByLabelText('Mùa anime').textContent).toContain('Mùa xuân');
    await user.click(screen.getByLabelText('Loại nội dung người lớn'));
    await user.click(screen.getByRole('option', { name: 'Không' }));
    expect(onChange).toHaveBeenCalledWith('exclude_adult', false);
  });
});

describe('YouTube target scopes', () => {
  const channel = 'UC_x5XG1OV2P6uZZ5FSM9Ttw';
  it('requires exactly one populated target family and ignores optional format filters', () => {
    for (const configuration of [
      { youtube_channel_id: channel },
      { youtube_channels: [channel, 'UCabcdefghijABCDEFGHIJ12'] },
      { youtube_handles: ['@Google'] },
      { youtube_playlist_ids: ['PL1234567890'] },
    ]) {
      expect(scopeGate('youtube', configuration)).toBe(true);
    }
    expect(scopeGate('youtube', { youtube_filter: 'all' })).toBe(false);
    expect(scopeGate('youtube', { youtube_channel_id: channel, youtube_handles: ['@Google'] })).toBe(false);
    expect(scopeGate('youtube', { youtube_channels: [] })).toBe(false);
  });

  it('retains raw separators in multi-target lists and resyncs authoritative resets', () => {
    const { onChange, rerender } = view('youtube', { youtube_handles: ['@Google'] });
    const field = screen.getByLabelText('Channel handles');
    fireEvent.change(field, { target: { value: '@Google, @NASA, ' } });
    expect(onChange).toHaveBeenCalledWith('youtube_handles', ['@Google', '@NASA']);
    rerender({ youtube_handles: ['@Google', '@NASA'] });
    expect(field).toHaveValue('@Google, @NASA, ');
    rerender({ youtube_handles: ['@Google', '@NASA'] }, 1);
    expect(field).toHaveValue('@Google, @NASA');
  });

  it('switches legacy targets to playlists with explicit clears and chooses Shorts', async () => {
    const user = userEvent.setup();
    const { onChange, rerender } = view('youtube', { youtube_channel_id: channel });
    await user.click(screen.getByLabelText('YouTube targets'));
    await user.click(screen.getByRole('option', { name: 'Playlist IDs' }));
    expect(onChange).toHaveBeenCalledWith('youtube_channel_id', null);
    expect(onChange).toHaveBeenCalledWith('youtube_playlist_ids', []);
    rerender({ youtube_playlist_ids: [] });
    fireEvent.change(screen.getByLabelText('Playlist IDs'), { target: { value: 'PL1234567890, PLabcdefghij' } });
    expect(onChange).toHaveBeenCalledWith('youtube_playlist_ids', ['PL1234567890', 'PLabcdefghij']);
    await user.click(screen.getByLabelText('Video format'));
    await user.click(screen.getByRole('option', { name: 'Shorts' }));
    expect(onChange).toHaveBeenCalledWith('youtube_filter', 'shorts');
  });
});

describe('media Configure serialization', () => {
  it('sends explicit null for cleared saved media options in ScheduleStep', async () => {
    const user = userEvent.setup();
    const changeConfiguration = vi.fn();
    const m = {
      provider: 'livechart_episodes',
      currentEntry: undefined,
      configuration: config({ media_genres: ['action'], release_year: 2026, anime_season: 'spring' }),
      builtinEntry: { label: 'LiveChart', applicable_options: ['media_genres', 'release_year', 'anime_season'] } as BuiltinSource,
      locked: false,
      showStep: () => true,
      changeConfiguration,
    } as unknown as ConnectorEditorModel;
    render(
      <NextIntlClientProvider locale="en-US" messages={{ sources: getCatalog('en-us', 'sources') }}>
        <ScheduleStep m={m} />
      </NextIntlClientProvider>,
    );
    fireEvent.click(screen.getByRole('checkbox', { name: 'Action' }));
    expect(changeConfiguration).toHaveBeenCalledWith('media_genres', null);
    fireEvent.change(screen.getByLabelText('Release year'), { target: { value: '' } });
    expect(changeConfiguration).toHaveBeenCalledWith('release_year', null);
    await user.click(screen.getByLabelText('Anime season'));
    await user.click(screen.getByRole('option', { name: 'Default' }));
    expect(changeConfiguration).toHaveBeenCalledWith('anime_season', null);
  });
});

describe('TVMaze scoped panel', () => {
  it('shows the show ID list and hides the country only in followed-shows mode', () => {
    const { unmount } = view('tvmaze_schedule', { tvmaze_mode: 'schedule_country', media_country: 'US' });
    expect(screen.queryByLabelText('TVMaze show IDs')).toBeNull();
    expect(screen.getByLabelText('Country')).toBeTruthy();
    unmount();
    view('tvmaze_schedule', { tvmaze_mode: 'followed_shows', tvmaze_show_ids: [1] });
    expect(screen.getByLabelText('TVMaze show IDs')).toBeTruthy();
    expect(screen.queryByLabelText('Country')).toBeNull();
    expect(screen.queryByLabelText('Global web channels')).toBeNull();
  });
  it('offers global channels only for the web schedule and clears the country when toggled on', async () => {
    const { unmount, onChange } = view('tvmaze_schedule', { tvmaze_mode: 'schedule_web', media_country: 'US' });
    expect(screen.getByLabelText('Global web channels')).not.toBeDisabled();
    await userEvent.click(screen.getByLabelText('Global web channels'));
    await userEvent.click(screen.getByRole('option', { name: 'Yes' }));
    expect(onChange).toHaveBeenCalledWith('tvmaze_global', true);
    expect(onChange).toHaveBeenCalledWith('media_country', null);
    unmount();
    view('tvmaze_schedule', { tvmaze_mode: 'schedule_web' });
    expect(screen.getByLabelText('Global web channels')).not.toBeDisabled();
  });
  it('clears dependent fields when the mode changes', async () => {
    const user = userEvent.setup();
    const { onChange } = view('tvmaze_schedule', { tvmaze_mode: 'followed_shows', tvmaze_show_ids: [1] });
    await user.click(screen.getByLabelText('TVMaze schedule'));
    await user.click(screen.getByRole('option', { name: 'Web schedule' }));
    expect(onChange).toHaveBeenCalledWith('tvmaze_mode', 'schedule_web');
    expect(onChange).toHaveBeenCalledWith('tvmaze_show_ids', null);
    expect(onChange).toHaveBeenCalledWith('tvmaze_global', null);
  });
  it('mirrors the server mode rules in the scope gate', () => {
    expect(scopeGate('tvmaze_schedule', { tvmaze_mode: 'followed_shows' })).toBe(false);
    expect(scopeGate('tvmaze_schedule', { tvmaze_mode: 'followed_shows', tvmaze_show_ids: [1, 2] })).toBe(true);
    expect(scopeGate('tvmaze_schedule', { tvmaze_mode: 'schedule_web', tvmaze_show_ids: [1] })).toBe(false);
    expect(scopeGate('tvmaze_schedule', { tvmaze_mode: 'schedule_country', tvmaze_global: true })).toBe(false);
    expect(scopeGate('tvmaze_schedule', { tvmaze_mode: 'schedule_web', tvmaze_global: true, media_country: 'US' })).toBe(false);
    expect(scopeGate('tvmaze_schedule', { tvmaze_mode: 'schedule_web', tvmaze_global: true })).toBe(true);
  });
  it('exposes the new round-3 filters on the media providers', () => {
    const fields = MEDIA_PROVIDER_FIELDS as Record<string, readonly string[]>;
    expect(fields.itunes_movies).toEqual(expect.arrayContaining(['release_year', 'min_rating']));
    expect(fields.letterboxd_rss).toContain('min_rating');
    expect(fields.apple_books_rss).toEqual(expect.arrayContaining(['content_ratings', 'release_year']));
    expect(fields.podcast_rss).toEqual(expect.arrayContaining(['content_ratings', 'media_formats']));
    expect(fields.openlibrary_books).toEqual(expect.arrayContaining(['release_year', 'media_genres']));
  });
});
