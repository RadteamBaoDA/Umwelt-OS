'use client';

import { useTranslations } from 'next-intl';
import type { ComponentType } from 'react';
import { Checkbox } from '@/components/ui/checkbox';
import { CheckboxField } from '@/components/settings-layout';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  inactiveMediaModeFilters,
  MAL_MANGA_RANKINGS,
  MEDIA_FILTER_SPECS,
  MEDIA_PROVIDER_FIELDS,
  OPTION_SPECS,
} from './builtin-option-specs';
import { TypedOption } from './builtin-options';
import type { ConnectorConfig } from './api';
import { CoinGeckoPicker, LocationButton, ScopeField, ScopeHint, MediaFilterFields, useIdListText } from './scope-fields';

export { ProviderGuide, ProviderTerms } from './provider-guide';
export { ScopeHint } from './scope-fields';

/** Providers with a dedicated native source scope, including media providers using `MediaScope`. */
export type NativeProvider =
  | 'tmdb_movies'
  | 'tmdb_tv'
  | 'trakt_media'
  | 'omdb_lookup'
  | 'mal_anime'
  | 'mal_manga'
  | 'google_books'
  | 'hardcover_books'
  | 'youtube_api'
  | 'podcast_index'
  | 'listenbrainz_releases'
  | 'youtube'
  | 'arxiv'
  | 'huggingface'
  | 'reddit'
  | 'bluesky'
  | 'mastodon'
  | 'x'
  | 'github'
  | 'github_releases'
  | 'telegram'
  | 'alpha_vantage'
  | 'coingecko'
  | 'open_meteo'
  | 'google_news'
  | 'sec_edgar'
  | 'openalex'
  | 'fred'
  | 'openweathermap'
  | 'openaq'
  | 'nasa_firms'
  | 'notion'
  | 'slack'
  | keyof typeof MEDIA_PROVIDER_FIELDS;

type ScopeChange = <K extends keyof ConnectorConfig>(key: K, value: ConnectorConfig[K]) => void;

/** Props every provider scope section receives; sections read only the configuration keys they own. */
export interface ScopeSectionProps {
  provider: NativeProvider;
  configuration: ConnectorConfig;
  disabled: boolean;
  sourceId?: string;
  credentialConfigured?: boolean;
  resetEpoch: number;
  onChange: ScopeChange;
}

const LAT_PATTERN = '-?[0-9]{1,2}(\\.[0-9]+)?';
const LON_PATTERN = '-?[0-9]{1,3}(\\.[0-9]+)?';
/** Bluesky handle (domain name) or did:plc / did:web identifier; kept as a JS string so the escape is a single backslash. */
const BLUESKY_ACTOR_PATTERN =
  '(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\.)+[A-Za-z]{2,63}|did:plc:[a-z2-7]{24}|did:web:[A-Za-z0-9.%:-]{1,220}';
/** Splits a comma/space separated list of terms. */
const splitTerms = (value: string) => value.split(/[\s,]+/).filter(Boolean);
/** Parses a numeric text field; an emptied field is `undefined`. */
const toNumber = (value: string) => (value === '' ? undefined : Number(value));

/** SEC EDGAR scope: CIK and the contact e-mail the SEC requires in the User-Agent. */
function SecEdgarScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <>
      <ScopeField
        name="sec_cik"
        label={t('sec.cik')}
        value={configuration.sec_cik ?? ''}
        onValue={(value) => onChange('sec_cik', value)}
        disabled={disabled}
        maxLength={10}
        pattern="[0-9]{10}"
      />
      <ScopeField
        name="sec_contact_email"
        label={t('sec.contactEmail')}
        value={configuration.sec_contact_email ?? ''}
        onValue={(value) => onChange('sec_contact_email', value)}
        disabled={disabled}
        maxLength={254}
      />
      <small className="text-muted-foreground">{t('sec.help')}</small>
    </>
  );
}

/** Google News scope: query, optional site restriction and result locale. */
function GoogleNewsScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <>
      <ScopeField
        name="news_query"
        label={t('news.query')}
        value={configuration.news_query ?? ''}
        onValue={(value) => onChange('news_query', value)}
        disabled={disabled}
        maxLength={200}
      />
      <div className="field">
        <Label htmlFor="source-scope-news_site">{t('news.site')}</Label>
        <Select
          value={configuration.news_site ?? 'any'}
          onValueChange={(value) => onChange('news_site', value as ConnectorConfig['news_site'])}
          disabled={disabled}
        >
          <SelectTrigger id="source-scope-news_site">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="any">{t('news.siteAny')}</SelectItem>
            {(['reuters.com', 'apnews.com', 'bbc.com', 'vnexpress.net'] as const).map((site) => (
              <SelectItem key={site} value={site}>
                {site}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="field">
        <Label htmlFor="source-scope-news_locale">{t('news.locale')}</Label>
        <Select
          value={configuration.news_locale ?? 'en-US'}
          onValueChange={(value) => onChange('news_locale', value as ConnectorConfig['news_locale'])}
          disabled={disabled}
        >
          <SelectTrigger id="source-scope-news_locale">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="en-US">{t('news.localeEn')}</SelectItem>
            <SelectItem value="vi-VN">{t('news.localeVi')}</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <small className="text-muted-foreground">{t('news.help')}</small>
    </>
  );
}

/** OpenAlex scope: one scholarly search query. */
function OpenAlexScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <>
      <ScopeField
        name="scholarly_query"
        label={t('scholarly.query')}
        value={configuration.scholarly_query ?? ''}
        onValue={(value) => onChange('scholarly_query', value)}
        disabled={disabled}
        maxLength={200}
      />
      <small className="text-muted-foreground">{t('scholarly.help')}</small>
    </>
  );
}

/** FRED scope: series id, upper-cased as typed. */
function FredScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <ScopeField
      name="fred_series_id"
      label={t('fredSeriesId')}
      value={configuration.fred_series_id ?? ''}
      onValue={(value) => onChange('fred_series_id', value.toUpperCase())}
      disabled={disabled}
      maxLength={25}
      pattern="[A-Z0-9_]{1,25}"
    />
  );
}

/** OpenWeatherMap scope: coordinates, fillable from the device location. */
function OpenWeatherMapScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <>
      <LocationButton
        disabled={disabled}
        onLocate={(latitude, longitude) => {
          onChange('owm_latitude', latitude);
          onChange('owm_longitude', longitude);
        }}
      />
      <ScopeField
        name="owm_latitude"
        label={t('weather.latitude')}
        value={String(configuration.owm_latitude ?? '')}
        onValue={(value) => onChange('owm_latitude', toNumber(value))}
        disabled={disabled}
        maxLength={24}
        pattern={LAT_PATTERN}
      />
      <ScopeField
        name="owm_longitude"
        label={t('weather.longitude')}
        value={String(configuration.owm_longitude ?? '')}
        onValue={(value) => onChange('owm_longitude', toNumber(value))}
        disabled={disabled}
        maxLength={24}
        pattern={LON_PATTERN}
      />
    </>
  );
}

/** OpenAQ scope: numeric location id. */
function OpenAqScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <ScopeField
      name="openaq_location_id"
      label={t('openaqLocationId')}
      value={String(configuration.openaq_location_id ?? '')}
      onValue={(value) => onChange('openaq_location_id', toNumber(value))}
      disabled={disabled}
      maxLength={10}
      pattern="[0-9]{1,10}"
    />
  );
}

/** NASA FIRMS scope: three-letter country code, upper-cased as typed. */
function NasaFirmsScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <ScopeField
      name="firms_country"
      label={t('firmsCountry')}
      value={configuration.firms_country ?? ''}
      onValue={(value) => onChange('firms_country', value.toUpperCase())}
      disabled={disabled}
      maxLength={3}
      pattern="[A-Z]{3}"
    />
  );
}

/**
 * Preserves legacy channel scopes and selects exactly one bounded target list (IDs, handles, or playlists).
 * Target-mode changes clear other identifiers explicitly; filter selection leaves target identity untouched.
 */
function YoutubeScope(props: ScopeSectionProps) {
  const { provider, configuration, disabled, resetEpoch, onChange } = props;
  const t = useTranslations('sources');
  const mode =
    configuration.youtube_channels != null
      ? 'youtube_channels'
      : configuration.youtube_handles != null
        ? 'youtube_handles'
        : configuration.youtube_playlist_ids != null
          ? 'youtube_playlist_ids'
          : 'youtube_channel_id';
  const channels = useIdListText(configuration.youtube_channels ?? [], resetEpoch);
  const handles = useIdListText(configuration.youtube_handles ?? [], resetEpoch);
  const playlists = useIdListText(configuration.youtube_playlist_ids ?? [], resetEpoch);
  const lists = { youtube_channels: channels, youtube_handles: handles, youtube_playlist_ids: playlists };
  const targetLabels = {
    youtube_channel_id: 'youtube.channelId',
    youtube_channels: 'youtube.channels',
    youtube_handles: 'youtube.handles',
    youtube_playlist_ids: 'youtube.playlists',
  } as const;
  return (
    <>
      <div className="field">
        <Label htmlFor="youtube-target-mode">{t('youtube.targetMode')}</Label>
        <Select
          value={mode}
          disabled={disabled}
          onValueChange={(next) => {
            // The backend accepts exactly one target family, including legacy single-channel configurations.
            for (const key of Object.keys(targetLabels) as (keyof typeof targetLabels)[]) {
              onChange(key, (key === next ? (key === 'youtube_channel_id' ? '' : []) : null) as never);
            }
            if (provider === 'youtube_api' && next === 'youtube_playlist_ids') {
              onChange('youtube_filter', 'all');
              onChange('media_countries', null as never);
            }
          }}
        >
          <SelectTrigger id="youtube-target-mode">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {(Object.keys(targetLabels) as (keyof typeof targetLabels)[]).map((key) => (
              <SelectItem key={key} value={key}>
                {t(targetLabels[key])}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {mode === 'youtube_channel_id' ? (
        <ScopeField
          name={mode}
          label={t('youtube.channelId')}
          value={configuration.youtube_channel_id ?? ''}
          onValue={(value) => onChange(mode, value)}
          disabled={disabled}
          maxLength={24}
          pattern="UC[A-Za-z0-9_-]{22}"
        />
      ) : (
        <ScopeField
          name={mode}
          label={t(targetLabels[mode])}
          value={lists[mode].value}
          disabled={disabled}
          maxLength={mode === 'youtube_channels' ? 208 : mode === 'youtube_handles' ? 264 : 528}
          onValue={(value) => onChange(mode, lists[mode].edit(value))}
        />
      )}
      <div className="field">
        <Label htmlFor="youtube-filter">{t('youtube.filter')}</Label>
        <Select
          value={configuration.youtube_filter ?? 'all'}
          disabled={disabled || (provider === 'youtube_api' && mode === 'youtube_playlist_ids')}
          onValueChange={(next) => onChange('youtube_filter', next as ConnectorConfig['youtube_filter'])}
        >
          <SelectTrigger id="youtube-filter">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t('youtube.all')}</SelectItem>
            <SelectItem value="long_form">{t('youtube.longForm')}</SelectItem>
            <SelectItem value="shorts">{t('youtube.shorts')}</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {provider === 'youtube_api' && (
        <MediaFilterFields
          {...props}
          fields={MEDIA_PROVIDER_FIELDS.youtube_api.filter((key) => mode !== 'youtube_playlist_ids' || key !== 'media_countries')}
        />
      )}
    </>
  );
}

/** Returns whether an Open Library field belongs to the currently selected search mode. */
function acceptsOpenLibraryField(key: string, configuration: ConnectorConfig): boolean {
  const mode = configuration.openlibrary_mode ?? 'subject';
  switch (key) {
    case 'openlibrary_subject':
      return mode === 'subject';
    case 'openlibrary_query':
    case 'media_languages':
      return mode === 'search';
    case 'ebooks_only':
      return mode !== 'recent';
    default:
      return true;
  }
}

/** Returns whether a Trakt query or calendar field matches the selected endpoint mode. */
function acceptsTraktField(key: string, configuration: ConnectorConfig): boolean | undefined {
  if (key === 'trakt_query') return (configuration.trakt_mode ?? 'movies_trending').endsWith('_search');
  if (key === 'trakt_calendar_days') return (configuration.trakt_mode ?? 'movies_trending').endsWith('_calendar');
  return undefined;
}

/** Returns whether an OMDb target field matches the selected lookup mode. */
function acceptsOmdbField(key: string, configuration: ConnectorConfig): boolean | undefined {
  if (key === 'omdb_imdb_id') return (configuration.omdb_mode ?? 'title') === 'imdb_id';
  if (key === 'omdb_title') return (configuration.omdb_mode ?? 'title') === 'title';
  if (key === 'omdb_search') return (configuration.omdb_mode ?? 'title') === 'search';
  return undefined;
}

/** Returns whether a provider mode permits one of its conditional fields to be shown. */
function acceptsProviderModeField(provider: NativeProvider, key: string, configuration: ConnectorConfig): boolean {
  if (key === 'mal_season' || key === 'mal_season_year') return configuration.mal_mode === 'season';
  if (key === 'mal_ranking_type') return configuration.mal_mode !== 'season';
  if (key === 'ann_media_type') return configuration.ann_mode === 'encyclopedia';
  if (key === 'tvmaze_global') return configuration.tvmaze_mode === 'schedule_web';
  if (key === 'tvmaze_show_ids') return configuration.tvmaze_mode === 'followed_shows';
  if (provider === 'tvmaze_schedule' && (key === 'media_country' || key === 'media_countries')) return !configuration.tvmaze_global;
  const traktField = acceptsTraktField(key, configuration);
  if (traktField !== undefined) return traktField;
  const omdbField = acceptsOmdbField(key, configuration);
  if (omdbField !== undefined) return omdbField;
  return true;
}

/** Selects the mode-aware visibility rule for one provider field. */
function acceptsMediaScopeField(provider: NativeProvider, key: string, configuration: ConnectorConfig): boolean {
  return provider === 'openlibrary_books'
    ? acceptsOpenLibraryField(key, configuration)
    : acceptsProviderModeField(provider, key, configuration);
}

/** Clears MAL fields from the inactive endpoint so they stay out of the source's identity. */
function clearMalTargets(key: string, next: unknown, onChange: ScopeChange): void {
  if (key !== 'mal_mode') return;
  if (next === 'season') onChange('mal_ranking_type', null as never);
  else {
    onChange('mal_season_year', null as never);
    onChange('mal_season', null as never);
  }
}

/** Clears fields that become invalid when a media provider's selected endpoint changes. */
function clearDependentMediaFields(key: string, next: unknown, onChange: ScopeChange): void {
  clearSteamNewsTargets(key, next, onChange);
  if (key === 'itunes_podcast_id' && next) onChange('feed_url', null as never);
  if (key === 'openlibrary_mode') {
    onChange('openlibrary_subject', null as never);
    onChange('openlibrary_query', null as never);
    onChange('media_languages', null as never);
    onChange('ebooks_only', null as never);
  }
  if (key === 'ann_mode' && next !== 'encyclopedia') onChange('ann_media_type', null as never);
  if (key === 'tvmaze_mode') onChange('tvmaze_global', null as never);
  clearTvmazeShowIds(key, next, onChange);
  if (key === 'tvmaze_global' && next === true) {
    onChange('media_country', null as never);
    onChange('media_countries', null as never);
  }
  if (key === 'trakt_mode') {
    if (typeof next !== 'string' || !next.endsWith('_search')) onChange('trakt_query', null as never);
    if (typeof next !== 'string' || !next.endsWith('_calendar')) onChange('trakt_calendar_days', null as never);
  }
  inactiveMediaModeFilters(key, next).forEach((inactive) => onChange(inactive, null as never));
  clearMalTargets(key, next, onChange);
  if (key === 'omdb_mode') {
    onChange('omdb_imdb_id', null as never);
    onChange('omdb_title', null as never);
    onChange('omdb_search', null as never);
  }
}

/**
 * Clears TVMaze show IDs when the endpoint leaves followed-shows mode, the only mode the server accepts them in
 * (a stale list would otherwise be sent and rejected with 422).
 */
function clearTvmazeShowIds(key: string, next: unknown, onChange: ScopeChange): void {
  if (key === 'tvmaze_mode' && next !== 'followed_shows') onChange('tvmaze_show_ids', null as never);
}

/** Clears Steam app IDs when leaving the only mode that consumes them. */
function clearSteamNewsTargets(key: string, next: unknown, onChange: ScopeChange): void {
  if (key === 'steam_mode' && next !== 'news') onChange('steam_appids', null as never);
}

/**
 * Renders media source targets and provider options, followed by optional shared media filters.
 * Clears incompatible target values when the owner changes endpoint modes.
 */
function MediaScope(props: ScopeSectionProps) {
  const t = useTranslations('sources');
  const { provider, configuration, disabled, resetEpoch, onChange } = props;
  const fields: readonly string[] = MEDIA_PROVIDER_FIELDS[provider as keyof typeof MEDIA_PROVIDER_FIELDS];
  const filters = MEDIA_FILTER_SPECS.map((spec) => spec.key);
  const inactive = new Set<string>(
    provider === 'tmdb_movies'
      ? inactiveMediaModeFilters('tmdb_mode', configuration.tmdb_mode)
      : inactiveMediaModeFilters('omdb_mode', provider === 'omdb_lookup' ? configuration.omdb_mode : undefined),
  );
  const accepted = fields.filter((key) => !inactive.has(key) && acceptsMediaScopeField(provider, key, configuration));
  const filtered =
    provider === 'podcast_index'
      ? accepted.filter((key) => {
          if (key === 'content_ratings') return Boolean(configuration.podcast_index_query);
          return (
            key === 'podcast_index_query' ||
            !key.startsWith('podcast_index_') ||
            (configuration.podcast_index_query ? key === 'podcast_index_value' : key !== 'podcast_index_value')
          );
        })
      : accepted;
  return (
    <>
      {provider === 'podcast_rss' && (
        <ScopeField
          name="feed_url"
          label={t('media.podcastFeed')}
          value={configuration.feed_url ?? ''}
          required={!configuration.itunes_podcast_id}
          maxLength={2048}
          disabled={disabled}
          onValue={(next) => {
            onChange('feed_url', (next || null) as never);
            if (next) onChange('itunes_podcast_id', null as never);
          }}
        />
      )}
      {OPTION_SPECS.filter((spec) => filtered.includes(spec.key) && !filters.includes(spec.key)).map((item) => {
        const tmdbModes =
          provider === 'tmdb_movies'
            ? (['now_playing', 'upcoming', 'changes'] as const)
            : provider === 'tmdb_tv'
              ? (['airing_today', 'on_the_air'] as const)
              : undefined;
        const values =
          item.key === 'tmdb_mode'
            ? tmdbModes
            : item.key === 'mal_ranking_type' && provider === 'mal_manga'
              ? MAL_MANGA_RANKINGS
              : undefined;
        const spec = values ? { ...item, values } : item;
        return (
          <TypedOption
            key={`${resetEpoch}-${spec.key}`}
            spec={spec}
            value={configuration[spec.key]}
            disabled={disabled}
            onValue={(next) => {
              onChange(spec.key, (next === undefined || (Array.isArray(next) && next.length === 0) ? null : next) as never);
              // Recent-feed filters and search value blocks belong to different Podcast Index endpoints.
              if (spec.key === 'podcast_index_query') {
                const cleared = next
                  ? ([
                      'podcast_index_languages',
                      'podcast_index_categories',
                      'podcast_index_excluded_categories',
                      'podcast_index_since',
                    ] as const)
                  : (['podcast_index_value', 'content_ratings'] as const);
                for (const key of cleared) onChange(key, null as never);
              }
              clearDependentMediaFields(spec.key, next, onChange);
            }}
          />
        );
      })}
      <MediaFilterFields {...props} fields={filtered} />
    </>
  );
}

/** Reddit scope: one subreddit (read-only access). */
function RedditScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <>
      <ScopeField
        name="reddit_subreddit"
        label={t('reddit.subreddit')}
        value={configuration.reddit_subreddit ?? ''}
        onValue={(value) => onChange('reddit_subreddit', value.trim())}
        disabled={disabled}
        maxLength={21}
        pattern="[A-Za-z0-9_]{2,21}"
      />
      <small className="text-muted-foreground">{t('reddit.readOnlyHelp')}</small>
    </>
  );
}

/** Bluesky scope: handle (domain) or DID. */
function BlueskyScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <ScopeField
      name="bluesky_actor"
      label={t('blueskyActor')}
      value={configuration.bluesky_actor ?? ''}
      onValue={(value) => onChange('bluesky_actor', value.trim())}
      disabled={disabled}
      maxLength={253}
      pattern={BLUESKY_ACTOR_PATTERN}
    />
  );
}

/** One optional text input with its hint, used by the mutually-exclusive scopes (Mastodon, X); an emptied value is sent as undefined. */
function OptionalScopeInput({
  name,
  label,
  value,
  required,
  maxLength,
  type,
  disabled,
  onValue,
}: {
  name: keyof ConnectorConfig;
  label: string;
  value: string;
  required: boolean;
  maxLength: number;
  type?: 'url';
  disabled: boolean;
  onValue: (raw: string) => void;
}) {
  return (
    <div className="field">
      <Label htmlFor={`source-scope-${name}`}>{label}</Label>
      <Input
        id={`source-scope-${name}`}
        type={type}
        required={required}
        maxLength={maxLength}
        disabled={disabled}
        value={value}
        aria-describedby={`source-hint-${name}`}
        onChange={(event) => onValue(event.target.value)}
      />
      <ScopeHint field={name} />
    </div>
  );
}

/** Mastodon scope. Account and hashtag are mutually exclusive on the server, so each is optional here and the empty one is sent as undefined. */
function MastodonScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <>
      <OptionalScopeInput
        name="mastodon_instance_url"
        type="url"
        label={t('mastodon.instanceUrl')}
        required
        maxLength={512}
        disabled={disabled}
        value={configuration.mastodon_instance_url ?? ''}
        onValue={(raw) => onChange('mastodon_instance_url', raw.trim() || undefined)}
      />
      <OptionalScopeInput
        name="mastodon_account"
        label={t('mastodon.account')}
        required={!configuration.mastodon_hashtag}
        maxLength={320}
        disabled={disabled}
        value={configuration.mastodon_account ?? ''}
        onValue={(raw) => onChange('mastodon_account', raw.trim() || undefined)}
      />
      <OptionalScopeInput
        name="mastodon_hashtag"
        label={t('mastodon.hashtag')}
        required={!configuration.mastodon_account}
        maxLength={100}
        disabled={disabled}
        value={configuration.mastodon_hashtag ?? ''}
        onValue={(raw) => onChange('mastodon_hashtag', raw.trim().replace(/^#/, '') || undefined)}
      />
      <small className="text-muted-foreground">{t('mastodon.help')}</small>
    </>
  );
}

/** X scope. The server requires exactly one of account or search query; each is optional here and the empty one is sent as undefined. */
function XScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <>
      <OptionalScopeInput
        name="x_account"
        label={t('x.account')}
        required={!configuration.x_query}
        maxLength={16}
        disabled={disabled}
        value={configuration.x_account ?? ''}
        onValue={(raw) => onChange('x_account', raw.trim().replace(/^@/, '') || undefined)}
      />
      <OptionalScopeInput
        name="x_query"
        label={t('x.query')}
        required={!configuration.x_account}
        maxLength={512}
        disabled={disabled}
        value={configuration.x_query ?? ''}
        onValue={(raw) => onChange('x_query', raw.trim() || undefined)}
      />
      <small className="text-muted-foreground">{t('x.scopeHelp')}</small>
    </>
  );
}

/** arXiv scope: category. */
function ArxivScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <ScopeField
      name="arxiv_category"
      label={t('arxiv.category')}
      value={configuration.arxiv_category ?? ''}
      onValue={(value) => onChange('arxiv_category', value)}
      disabled={disabled}
      maxLength={64}
      pattern="[A-Za-z][A-Za-z0-9.-]{0,63}"
    />
  );
}

/** Hugging Face scope: author. */
function HuggingFaceScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <ScopeField
      name="huggingface_author"
      label={t('huggingface.author')}
      value={configuration.huggingface_author ?? ''}
      onValue={(value) => onChange('huggingface_author', value)}
      disabled={disabled}
      maxLength={96}
      pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,95}"
    />
  );
}

const GITHUB_INCLUDES = [
  ['include_issues', 'github.issues'],
  ['include_pulls', 'github.pulls'],
  ['include_commits', 'github.commits'],
  ['include_releases', 'github.releases'],
] as const;

/** GitHub-only scope extras: which activity kinds to include (commits/releases default on) and the history window in days. */
function GithubActivityOptions({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <>
      {GITHUB_INCLUDES.map(([key, label]) => (
        <CheckboxField key={key}>
          <Checkbox
            id={`source-scope-${key}`}
            disabled={disabled}
            checked={configuration[key] ?? (key === 'include_commits' || key === 'include_releases')}
            onCheckedChange={(checked) => onChange(key, checked === true)}
          />
          <Label htmlFor={`source-scope-${key}`}>{t(label)}</Label>
        </CheckboxField>
      ))}
      <div className="field">
        <Label htmlFor="source-scope-github_history_days">{t('github.historyDays')}</Label>
        <Input
          id="source-scope-github_history_days"
          type="number"
          min={1}
          max={365}
          value={configuration.github_history_days ?? 90}
          disabled={disabled}
          aria-describedby="source-hint-github_history_days"
          onChange={(event) => onChange('github_history_days', Number(event.target.value))}
        />
        <ScopeHint field="github_history_days" />
      </div>
    </>
  );
}

/** GitHub / GitHub releases scope: owner and repository; the full GitHub provider adds activity options. */
function GithubScope(props: ScopeSectionProps) {
  const t = useTranslations('sources');
  const { configuration, disabled, onChange, provider } = props;
  return (
    <>
      <ScopeField
        name="github_owner"
        label={t('github.owner')}
        value={configuration.github_owner ?? ''}
        onValue={(value) => onChange('github_owner', value)}
        disabled={disabled}
        maxLength={39}
        pattern="[A-Za-z0-9][A-Za-z0-9-]{0,38}"
      />
      <ScopeField
        name="github_repository"
        label={t('github.repository')}
        value={configuration.github_repository ?? ''}
        onValue={(value) => onChange('github_repository', value)}
        disabled={disabled}
        maxLength={100}
        pattern="[A-Za-z0-9_.-]{1,100}"
      />
      {provider === 'github' && <GithubActivityOptions {...props} />}
    </>
  );
}

/** Alpha Vantage scope: up to five symbols, quote currency and exchange timezone. */
function AlphaVantageScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <>
      <ScopeField
        name="market_symbols"
        label={t('market.symbols')}
        value={(configuration.market_symbols ?? []).join(', ')}
        onValue={(value) => onChange('market_symbols', splitTerms(value).slice(0, 5))}
        disabled={disabled}
        maxLength={110}
      />
      <ScopeField
        name="market_currency"
        label={t('market.currency')}
        value={configuration.market_currency ?? ''}
        onValue={(value) => onChange('market_currency', value.toUpperCase())}
        disabled={disabled}
        maxLength={3}
        pattern="[A-Z]{3}"
      />
      <ScopeField
        name="market_exchange_timezone"
        label={t('market.exchangeTimezone')}
        value={configuration.market_exchange_timezone ?? ''}
        onValue={(value) => onChange('market_exchange_timezone', value)}
        disabled={disabled}
        maxLength={64}
      />
    </>
  );
}

/** CoinGecko scope: asset picker backed by the keyed coin search. */
function CoinGeckoScope({ configuration, disabled, sourceId, credentialConfigured, onChange }: ScopeSectionProps) {
  return (
    <CoinGeckoPicker
      sourceId={sourceId ?? ''}
      credentialConfigured={credentialConfigured ?? false}
      disabled={disabled}
      assets={configuration.coingecko_assets}
      onChange={(assets) => onChange('coingecko_assets', assets)}
    />
  );
}

/** Open-Meteo scope: coordinates (fillable from the device, which also sets the browser timezone), timezone and metrics. */
function OpenMeteoScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return (
    <>
      <LocationButton
        disabled={disabled}
        onLocate={(latitude, longitude) => {
          onChange('weather_latitude', latitude);
          onChange('weather_longitude', longitude);
          // The browser zone matches the located device, so it replaces the default forecast timezone.
          onChange('weather_timezone', Intl.DateTimeFormat().resolvedOptions().timeZone);
        }}
      />
      <ScopeField
        name="weather_latitude"
        label={t('weather.latitude')}
        value={String(configuration.weather_latitude ?? '')}
        onValue={(value) => onChange('weather_latitude', toNumber(value))}
        disabled={disabled}
        maxLength={24}
        pattern={LAT_PATTERN}
      />
      <ScopeField
        name="weather_longitude"
        label={t('weather.longitude')}
        value={String(configuration.weather_longitude ?? '')}
        onValue={(value) => onChange('weather_longitude', toNumber(value))}
        disabled={disabled}
        maxLength={24}
        pattern={LON_PATTERN}
      />
      <ScopeField
        name="weather_timezone"
        label={t('weather.timezone')}
        value={configuration.weather_timezone ?? ''}
        onValue={(value) => onChange('weather_timezone', value)}
        disabled={disabled}
        maxLength={64}
      />
      <ScopeField
        name="weather_metrics"
        label={t('weather.metrics')}
        value={(configuration.weather_metrics ?? []).join(', ')}
        onValue={(value) => onChange('weather_metrics', splitTerms(value).slice(0, 4))}
        disabled={disabled}
        maxLength={200}
      />
    </>
  );
}

/** Id-list scopes (Notion pages, Slack channels, Telegram chats) share one raw-text field; only keys and messages differ. */
const ID_LIST_SCOPES = {
  notion: {
    key: 'notion_page_ids',
    label: 'notion.pageIds',
    help: 'notion.pageIdsHelp',
    helpId: 'source-scope-notion-help',
    maxLength: 13000,
  },
  slack: {
    key: 'slack_channel_ids',
    label: 'slack.channelIds',
    help: 'slack.channelIdsHelp',
    helpId: 'source-scope-slack-help',
    maxLength: 8400,
  },
  telegram: {
    key: 'telegram_chat_ids',
    label: 'telegram.channelIds',
    help: 'telegram.channelHelp',
    helpId: 'source-scope-telegram-help',
    maxLength: 2200,
  },
} as const;

/** Raw-text id list field bound to one configuration key; see `useIdListText` for the sync rules with the draft. */
function IdListScope({
  spec,
  configuration,
  disabled,
  resetEpoch,
  onChange,
}: ScopeSectionProps & { spec: (typeof ID_LIST_SCOPES)[keyof typeof ID_LIST_SCOPES] }) {
  const t = useTranslations('sources');
  const text = useIdListText(configuration[spec.key] ?? [], resetEpoch);
  return (
    <div className="field">
      <Label htmlFor={`source-scope-${spec.key}`}>{t(spec.label)}</Label>
      <Input
        id={`source-scope-${spec.key}`}
        maxLength={spec.maxLength}
        disabled={disabled}
        required
        value={text.value}
        aria-describedby={spec.helpId}
        onChange={(event) => onChange(spec.key, text.edit(event.target.value))}
      />
      <small id={spec.helpId} className="text-muted-foreground">
        {t(spec.help)}
      </small>
    </div>
  );
}

/**
 * Provider -> scope section registry. A new native provider plugs in by adding one entry here (and its name to
 * `NativeProvider`); `ProviderScope` itself does not change.
 */
const SCOPE_SECTIONS: Record<NativeProvider, ComponentType<ScopeSectionProps>> = {
  tmdb_movies: MediaScope,
  tmdb_tv: MediaScope,
  trakt_media: MediaScope,
  omdb_lookup: MediaScope,
  mal_anime: MediaScope,
  mal_manga: MediaScope,
  google_books: MediaScope,
  hardcover_books: MediaScope,
  podcast_index: MediaScope,
  listenbrainz_releases: MediaScope,
  steam_releases: MediaScope,
  igdb_games: MediaScope,
  youtube_api: YoutubeScope,
  anilist_anime: MediaScope,
  anilist_manga: MediaScope,
  livechart_episodes: MediaScope,
  tvmaze_schedule: MediaScope,
  openlibrary_books: MediaScope,
  gutendex_books: MediaScope,
  ann_news: MediaScope,
  mangaupdates_releases: MediaScope,
  mangadex_releases: MediaScope,
  apple_books_rss: MediaScope,
  podcast_rss: MediaScope,
  wikidata_movies: MediaScope,
  itunes_movies: MediaScope,
  letterboxd_rss: MediaScope,
  sec_edgar: SecEdgarScope,
  google_news: GoogleNewsScope,
  openalex: OpenAlexScope,
  fred: FredScope,
  openweathermap: OpenWeatherMapScope,
  openaq: OpenAqScope,
  nasa_firms: NasaFirmsScope,
  youtube: YoutubeScope,
  reddit: RedditScope,
  bluesky: BlueskyScope,
  mastodon: MastodonScope,
  x: XScope,
  arxiv: ArxivScope,
  huggingface: HuggingFaceScope,
  github: GithubScope,
  github_releases: GithubScope,
  alpha_vantage: AlphaVantageScope,
  coingecko: CoinGeckoScope,
  open_meteo: OpenMeteoScope,
  notion: (props) => <IdListScope {...props} spec={ID_LIST_SCOPES.notion} />,
  slack: (props) => <IdListScope {...props} spec={ID_LIST_SCOPES.slack} />,
  telegram: (props) => <IdListScope {...props} spec={ID_LIST_SCOPES.telegram} />,
};

/** Renders bounded native-provider scopes, including Reddit subreddits and selected Notion pages/Slack channels; values stay in the revision-fenced draft. */
export function ProviderScope(props: ScopeSectionProps) {
  const Section = SCOPE_SECTIONS[props.provider] ?? SCOPE_SECTIONS.telegram;
  return <Section {...props} />;
}
