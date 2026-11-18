/**
 * Literal `sources` catalog keys of the setup-guide steps per provider id (`generic` and `genericKeyed` are the
 * fallbacks). Tables instead of runtime key building keep every key type-checked; add a row with each new guide.
 */
export const GUIDE_STEP_KEYS = {
  tmdb_movies: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  tmdb_tv: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  trakt_media: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  omdb_lookup: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  mal_anime: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  mal_manga: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  google_books: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  hardcover_books: ['guideSteps.hardcover.s1', 'guideSteps.hardcover.s2', 'guideSteps.media.s3'],
  youtube_api: ['guideSteps.youtubeApi.s1', 'guideSteps.youtubeApi.s2', 'guideSteps.media.s3'],
  podcast_index: ['guideSteps.podcastIndex.s1', 'guideSteps.podcastIndex.s2', 'guideSteps.media.s3'],
  listenbrainz_releases: ['guideSteps.listenbrainz.s1', 'guideSteps.listenbrainz.s2', 'guideSteps.media.s3'],
  steam_releases: ['guideSteps.steam.s1', 'guideSteps.steam.s2', 'guideSteps.media.s3'],
  igdb_games: ['guideSteps.igdb.s1', 'guideSteps.igdb.s2', 'guideSteps.media.s3'],
  anilist_anime: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  anilist_manga: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  livechart_episodes: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  ann_news: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  mangaupdates_releases: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  mangadex_releases: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  openlibrary_books: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  gutendex_books: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  apple_books_rss: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  wikidata_movies: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  itunes_movies: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  letterboxd_rss: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  podcast_rss: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],
  tvmaze_schedule: ['guideSteps.media.s1', 'guideSteps.media.s2', 'guideSteps.media.s3'],

  generic: ['guideSteps.generic.s1', 'guideSteps.generic.s2', 'guideSteps.generic.s3', 'guideSteps.generic.s4'],
  genericKeyed: ['guideSteps.genericKeyed.s1', 'guideSteps.genericKeyed.s2', 'guideSteps.genericKeyed.s3', 'guideSteps.genericKeyed.s4'],
  rss: ['guideSteps.rss.s1', 'guideSteps.rss.s2', 'guideSteps.rss.s3', 'guideSteps.rss.s4'],
  rest: ['guideSteps.rest.s1', 'guideSteps.rest.s2', 'guideSteps.rest.s3', 'guideSteps.rest.s4'],
  web: ['guideSteps.web.s1', 'guideSteps.web.s2', 'guideSteps.web.s3', 'guideSteps.web.s4'],
  mcp: ['guideSteps.mcp.s1', 'guideSteps.mcp.s2', 'guideSteps.mcp.s3', 'guideSteps.mcp.s4'],
  youtube: ['guideSteps.youtube.s1', 'guideSteps.youtube.s2', 'guideSteps.youtube.s3'],
  arxiv: ['guideSteps.arxiv.s1', 'guideSteps.arxiv.s2', 'guideSteps.arxiv.s3'],
  huggingface: ['guideSteps.huggingface.s1', 'guideSteps.huggingface.s2', 'guideSteps.huggingface.s3'],
  github_releases: ['guideSteps.github_releases.s1', 'guideSteps.github_releases.s2', 'guideSteps.github_releases.s3'],
  github: ['guideSteps.github.s1', 'guideSteps.github.s2', 'guideSteps.github.s3', 'guideSteps.github.s4'],
  telegram: ['guideSteps.telegram.s1', 'guideSteps.telegram.s2', 'guideSteps.telegram.s3', 'guideSteps.telegram.s4'],
  alpha_vantage: [
    'guideSteps.alpha_vantage.s1',
    'guideSteps.alpha_vantage.s2',
    'guideSteps.alpha_vantage.s3',
    'guideSteps.alpha_vantage.s4',
    'guideSteps.alpha_vantage.s5',
  ],
  open_meteo: ['guideSteps.open_meteo.s1', 'guideSteps.open_meteo.s2', 'guideSteps.open_meteo.s3', 'guideSteps.open_meteo.s4'],
  google_news: ['guideSteps.google_news.s1', 'guideSteps.google_news.s2', 'guideSteps.google_news.s3', 'guideSteps.google_news.s4'],
  sec_edgar: ['guideSteps.sec_edgar.s1', 'guideSteps.sec_edgar.s2', 'guideSteps.sec_edgar.s3'],
  openalex: ['guideSteps.openalex.s1', 'guideSteps.openalex.s2', 'guideSteps.openalex.s3'],
  coingecko: ['guideSteps.coingecko.s1', 'guideSteps.coingecko.s2', 'guideSteps.coingecko.s3', 'guideSteps.coingecko.s4'],
  fred: ['guideSteps.fred.s1', 'guideSteps.fred.s2', 'guideSteps.fred.s3', 'guideSteps.fred.s4'],
  finnhub_news: ['guideSteps.finnhub_news.s1', 'guideSteps.finnhub_news.s2', 'guideSteps.finnhub_news.s3'],
  newsapi: ['guideSteps.newsapi.s1', 'guideSteps.newsapi.s2', 'guideSteps.newsapi.s3'],
  openweathermap: [
    'guideSteps.openweathermap.s1',
    'guideSteps.openweathermap.s2',
    'guideSteps.openweathermap.s3',
    'guideSteps.openweathermap.s4',
  ],
  openaq: ['guideSteps.openaq.s1', 'guideSteps.openaq.s2', 'guideSteps.openaq.s3', 'guideSteps.openaq.s4'],
  nasa_firms: ['guideSteps.nasa_firms.s1', 'guideSteps.nasa_firms.s2', 'guideSteps.nasa_firms.s3', 'guideSteps.nasa_firms.s4'],
} as const;

/** Literal `sources` catalog key of the input rule shown under each typed source field (see `ScopeHint`). */
export const SCOPE_HINT_KEY = {
  youtube_channels: 'scopeHint.youtube_channels',
  youtube_handles: 'scopeHint.youtube_handles',
  youtube_playlist_ids: 'scopeHint.youtube_playlist_ids',
  letterboxd_username: 'scopeHint.letterboxd_username',
  search_terms: 'scopeHint.search_terms',
  openlibrary_subject: 'scopeHint.openlibrary_subject',
  openlibrary_query: 'scopeHint.openlibrary_query',

  news_query: 'scopeHint.news_query',
  scholarly_query: 'scopeHint.scholarly_query',
  sec_cik: 'scopeHint.sec_cik',
  sec_contact_email: 'scopeHint.sec_contact_email',
  fred_series_id: 'scopeHint.fred_series_id',
  owm_latitude: 'scopeHint.owm_latitude',
  owm_longitude: 'scopeHint.owm_longitude',
  weather_latitude: 'scopeHint.weather_latitude',
  weather_longitude: 'scopeHint.weather_longitude',
  weather_timezone: 'scopeHint.weather_timezone',
  weather_metrics: 'scopeHint.weather_metrics',
  openaq_location_id: 'scopeHint.openaq_location_id',
  firms_country: 'scopeHint.firms_country',
  youtube_channel_id: 'scopeHint.youtube_channel_id',
  arxiv_category: 'scopeHint.arxiv_category',
  huggingface_author: 'scopeHint.huggingface_author',
  bluesky_actor: 'scopeHint.bluesky_actor',
  reddit_subreddit: 'scopeHint.reddit_subreddit',
  mastodon_instance_url: 'scopeHint.mastodon_instance_url',
  mastodon_account: 'scopeHint.mastodon_account',
  mastodon_hashtag: 'scopeHint.mastodon_hashtag',
  x_account: 'scopeHint.x_account',
  x_query: 'scopeHint.x_query',
  github_owner: 'scopeHint.github_owner',
  github_repository: 'scopeHint.github_repository',
  github_history_days: 'scopeHint.github_history_days',
  market_symbols: 'scopeHint.market_symbols',
  market_currency: 'scopeHint.market_currency',
  market_exchange_timezone: 'scopeHint.market_exchange_timezone',
  feed_url: 'scopeHint.feed_url',
  url: 'scopeHint.url',
  items_path: 'scopeHint.items_path',
  id_field: 'scopeHint.id_field',
  title_field: 'scopeHint.title_field',
  content_field: 'scopeHint.content_field',
  updated_field: 'scopeHint.updated_field',
  fhir_patient_id: 'scopeHint.fhir_patient_id',
  timezone: 'scopeHint.timezone',
} as const;

/** Any setup-guide step catalog key. */
export type GuideStepKey = (typeof GUIDE_STEP_KEYS)[keyof typeof GUIDE_STEP_KEYS][number];

/**
 * Setup-guide step keys for a provider, in order.
 * @param providerId Connector provider id; providers without a dedicated guide use the generic one.
 * @param keyed Whether the provider needs an owner-entered key (picks `genericKeyed` over `generic`).
 * @returns Literal catalog keys, never empty.
 */
export function guideStepKeys(providerId: string, keyed: boolean): readonly GuideStepKey[] {
  const own = Object.hasOwn(GUIDE_STEP_KEYS, providerId) ? GUIDE_STEP_KEYS[providerId as keyof typeof GUIDE_STEP_KEYS] : undefined;
  return own ?? (keyed ? GUIDE_STEP_KEYS.genericKeyed : GUIDE_STEP_KEYS.generic);
}

/** Catalog key of the input rule for one source field, or `undefined` when the field has no rule. */
export function scopeHintKey(field: string): (typeof SCOPE_HINT_KEY)[keyof typeof SCOPE_HINT_KEY] | undefined {
  return Object.hasOwn(SCOPE_HINT_KEY, field) ? SCOPE_HINT_KEY[field as keyof typeof SCOPE_HINT_KEY] : undefined;
}
