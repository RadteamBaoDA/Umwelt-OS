import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';
import type { ConnectorConfig } from './api';
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
  return { onChange, rerender: (next: Partial<ConnectorConfig>, epoch = 0) => result.rerender(tree(next, epoch)) };
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
      expect(container.querySelectorAll('[required]')).toHaveLength(0);
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

  it('allows empty optional filters but requires identifying source targets', () => {
    for (const provider of [
      'anilist_anime',
      'anilist_manga',
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
