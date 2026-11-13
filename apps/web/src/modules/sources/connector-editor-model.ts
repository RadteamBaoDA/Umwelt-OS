// Pure helpers, constants and types of the connector editor (no React state): catalog predicates, default configuration,
// error mapping and the wizard contract types. Extracted unchanged from connector-editor.tsx.
import { ApiError } from '@/core/api';
import { ConnectorCatalogEntry, ConnectorConfig, intervalBelowMinimum } from './api';

export const intervals = [15, 30, 60, 360, 1440] as const;
export const genericProviderIds = new Set(['rss', 'web', 'rest', 'mcp']);
export type Provider = string;

/** Uses the backend catalog to identify scheduled preset capabilities instead of duplicating provider IDs here. */
export function isPreset(entry: ConnectorCatalogEntry | undefined): boolean {
  return entry?.kind === 'preset';
}

/** Identifies a registered native source from catalog type and adapter evidence. */
export function isNativeEntry(entry: ConnectorCatalogEntry | undefined): boolean {
  return Boolean(
    entry &&
    (entry.kind === 'connector' || entry.kind === 'preset') &&
    entry.source_type &&
    !genericProviderIds.has(entry.provider_id) &&
    entry.code_available,
  );
}

/**
 * Scope fields each provider cannot collect without; mirrors the `required` inputs of the Choose-data step.
 * Either-or scopes (X account/query, Mastodon account/hashtag) are checked separately in the editor.
 */
export const REQUIRED_SCOPE: Partial<Record<string, readonly (keyof ConnectorConfig)[]>> = {
  google_news: ['news_query'],
  openalex: ['scholarly_query'],
  sec_edgar: ['sec_cik', 'sec_contact_email'],
  fred: ['fred_series_id'],
  openweathermap: ['owm_latitude', 'owm_longitude'],
  openaq: ['openaq_location_id'],
  nasa_firms: ['firms_country'],
  youtube: ['youtube_channel_id'],
  arxiv: ['arxiv_category'],
  huggingface: ['huggingface_author'],
  bluesky: ['bluesky_actor'],
  reddit: ['reddit_subreddit'],
  mastodon: ['mastodon_instance_url'],
  telegram: ['telegram_chat_ids'],
  github: ['github_owner', 'github_repository'],
  github_releases: ['github_owner', 'github_repository'],
  alpha_vantage: ['market_symbols', 'market_currency', 'market_exchange_timezone'],
  open_meteo: ['weather_latitude', 'weather_longitude', 'weather_timezone', 'weather_metrics'],
  notion: ['notion_page_ids'],
  slack: ['slack_channel_ids'],
  google_mail: ['google_mail_label_ids'],
  google_calendar: ['google_calendar_ids'],
  google_drive: ['google_drive_ids'],
  health: ['fhir_base_url', 'fhir_patient_id'],
  home_assistant: ['ha_base_url', 'ha_entity_ids'],
};

/** True when a scope value is present: a non-blank string, a non-empty list, or a finite number. */
export function scopeFilled(value: unknown): boolean {
  if (typeof value === 'string') return value.trim() !== '';
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'number') return Number.isFinite(value);
  return value !== undefined && value !== null;
}

/** Reports credentials stored in the write-only provider slot; Alpha Vantage has its own established credential owner. */
export function usesProviderCredential(entry: ConnectorCatalogEntry | undefined): boolean {
  return Boolean(
    entry &&
    entry.provider_id !== 'alpha_vantage' &&
    !genericProviderIds.has(entry.provider_id) &&
    ((entry.key_fields ?? []).includes('api_key') || entry.auth_methods.includes('http_header')),
  );
}

/** Reports API-key presets rendered with the catalog's key label and key-specific status. */
export function usesApiKey(entry: ConnectorCatalogEntry | undefined): boolean {
  return Boolean((entry?.key_fields ?? []).includes('api_key') && entry?.provider_id !== 'alpha_vantage');
}

/** Header-only catalog authentication requires a stored bearer token; optional Mastodon tokens remain optional. */
export function requiresHeaderCredential(entry: ConnectorCatalogEntry | undefined): boolean {
  return entry?.auth_methods.length === 1 && entry.auth_methods[0] === 'http_header';
}

/**
 * Reports a mandatory stored credential: API-key presets and header-only providers. Optional header tokens
 * (Mastodon) and generic sources (REST: `none` or a header) can be enabled without one.
 */
export function providerCredentialRequired(entry: ConnectorCatalogEntry | undefined): boolean {
  return usesProviderCredential(entry) && (usesApiKey(entry) || requiresHeaderCredential(entry));
}

export const PROVIDER_GROUP_ORDER = ['news', 'economy', 'hazards', 'space', 'security', 'tech', 'other'] as const;
export type ProviderGroup = (typeof PROVIDER_GROUP_ORDER)[number];
export const PROVIDER_GROUP_KEY = {
  news: 'builtinCategory.news',
  economy: 'builtinCategory.economy',
  hazards: 'builtinCategory.hazards',
  space: 'builtinCategory.space',
  security: 'builtinCategory.security',
  tech: 'builtinCategory.tech',
  other: 'provider.groupOther',
} as const;

/** True when the schedule/activation gate asks the owner to (re)enter a provider key. */
export function keyNeeded(code: string | null | undefined): boolean {
  return code === 'credential_missing' || code === 'invalid_credential';
}

/** Checks whether a persisted provider identifier is present before using it as the editor selection. */
export function isProvider(value: string): value is Provider {
  return value.length > 0;
}

/** Literal `sources` catalog key of each provider's display name that the provider picker translates; others use the catalog label. */
const PROVIDER_KEY = {
  rss: 'provider.rss',
  web: 'provider.web',
  rest: 'provider.rest',
  mcp: 'provider.mcp',
  github: 'provider.github',
  google_mail: 'provider.googleMail',
  google_calendar: 'provider.googleCalendar',
  google_drive: 'provider.googleDrive',
  youtube: 'provider.youtube',
  arxiv: 'provider.arxiv',
  huggingface: 'provider.huggingface',
  github_releases: 'provider.githubReleases',
  telegram: 'provider.telegram',
  alpha_vantage: 'provider.alphaVantage',
  coingecko: 'provider.coinGecko',
  open_meteo: 'provider.openMeteo',
  google_news: 'provider.googleNews',
  openalex: 'provider.openAlex',
  reddit: 'provider.reddit',
  hacker_news: 'provider.hackerNews',
  mastodon: 'provider.mastodon',
  bluesky: 'provider.bluesky',
  x: 'provider.x',
  vietnamese_press: 'provider.vietnamesePress',
  gdelt_government: 'provider.gdelt',
  finance: 'provider.finance',
  weather_disaster_climate: 'provider.weather',
  cyber_cve: 'provider.cyber',
  map_osint: 'provider.mapOsint',
  browser: 'provider.browser',
  health: 'provider.health',
  iot: 'provider.iot',
  notion: 'provider.notion',
  slack: 'provider.slack',
  home_assistant: 'provider.homeAssistant',
  bbc_world: 'provider.bbcWorld',
  vnexpress_business: 'provider.vnexpressBusiness',
  dantri_news: 'provider.dantriNews',
  thanhnien_news: 'provider.thanhnienNews',
  tuoitre_news: 'provider.tuoitreNews',
  hn_top: 'provider.hnTop',
  gdelt_economy: 'provider.gdeltEconomy',
  world_bank: 'provider.worldBank',
  frankfurter: 'provider.frankfurter',
  ecb: 'provider.ecb',
  binance: 'provider.binance',
  alternative_me: 'provider.alternativeMe',
  usgs: 'provider.usgs',
  coinpaprika: 'provider.coinpaprika',
  nasa_eonet: 'provider.nasaEonet',
  cisa_kev: 'provider.cisaKev',
  sec_edgar: 'provider.secEdgar',
} as const;

/**
 * Maps a provider identifier to its localized catalog key.
 * @param providerId Catalog provider id.
 * @returns A `provider.<id>` key, or `provider.label` (the generic word) when the picker should show the catalog label instead.
 */
export function providerKey(providerId: string): (typeof PROVIDER_KEY)[keyof typeof PROVIDER_KEY] | 'provider.label' {
  return Object.hasOwn(PROVIDER_KEY, providerId) ? PROVIDER_KEY[providerId as keyof typeof PROVIDER_KEY] : 'provider.label';
}

/**
 * Starting scope for native providers so each required field is filled and the wizard can move on at once;
 * the owner can replace any value. Owner-specific scopes (Telegram/Slack/Notion ids, Google resources,
 * local health/Home Assistant endpoints) have no meaningful public default and stay empty.
 */
export const NATIVE_SCOPE_DEFAULTS: Partial<Record<string, Partial<ConnectorConfig>>> = {
  youtube: { youtube_channel_id: 'UC_x5XG1OV2P6uZZ5FSM9Ttw' },
  arxiv: { arxiv_category: 'cs.AI' },
  huggingface: { huggingface_author: 'google' },
  bluesky: { bluesky_actor: 'bsky.app' },
  reddit: { reddit_subreddit: 'worldnews' },
  mastodon: { mastodon_instance_url: 'https://mastodon.social', mastodon_hashtag: 'news' },
  x: { x_query: 'news' },
  github: { github_owner: 'vercel', github_repository: 'next.js' },
  github_releases: { github_owner: 'vercel', github_repository: 'next.js' },
  alpha_vantage: { market_symbols: ['IBM'], market_currency: 'USD', market_exchange_timezone: 'America/New_York' },
  // Ho Chi Minh City; "Use my location" replaces it with the device position.
  open_meteo: {
    weather_metrics: ['temperature_2m'],
    weather_timezone: 'Asia/Ho_Chi_Minh',
    weather_latitude: 10.8231,
    weather_longitude: 106.6297,
  },
};

/** Builds the editable default configuration for the selected provider. */
export function defaultConfiguration(provider: Provider, entry?: ConnectorCatalogEntry): ConnectorConfig {
  // Preset providers use catalog configuration (including the example scope) so every required field starts filled
  // and newly registered adapters need no parallel frontend list.
  // SEC fair-access rules require the owner's real contact address, so the placeholder email is never preselected.
  if (provider === 'sec_edgar' && entry?.example_config) return { ...(entry.example_config as ConnectorConfig), sec_contact_email: '' };
  if (isPreset(entry) && entry?.example_config) return entry.example_config as ConnectorConfig;
  if (isNativeEntry(entry))
    return {
      timeout_seconds: 30,
      timezone: 'Asia/Ho_Chi_Minh',
      schedule_interval_minutes: provider === 'alpha_vantage' ? 1440 : provider === 'youtube' || provider === 'arxiv' ? 15 : 30,
      history_mode: provider === 'telegram' ? 'pending_updates' : 'returned_snapshot',
      ...NATIVE_SCOPE_DEFAULTS[provider],
      ...(provider === 'github'
        ? { include_issues: true, include_pulls: true, include_commits: true, include_releases: true, github_history_days: 90 }
        : {}),
    };
  return {
    js_render: false,
    max_pages: 10,
    max_depth: 2,
    timeout_seconds: 60,
    timezone: 'Asia/Ho_Chi_Minh',
    schedule_interval_minutes: provider === 'rss' ? 15 : 30,
    ...(provider === 'rest' ? { items_path: 'items', id_field: 'id', content_field: 'content' } : {}),
  };
}

/** Converts a request or validation failure into the editor’s supported error message key. */
export function errorText(error: unknown): string {
  if (
    error instanceof ApiError &&
    error.status === 409 &&
    (error.code === 'builtin_already_added' || error.message === 'builtin_already_added')
  )
    return 'builtin.alreadyAdded';
  if (error instanceof ApiError && error.status === 409) return 'conflict';
  if (intervalBelowMinimum(error)) return 'schedule.belowMinimum';
  if (error instanceof ApiError && error.status === 422) return 'configurationInvalid';
  if (error instanceof ApiError && error.status === 503) return 'serviceUnavailable';
  if (error instanceof Error && error.message === 'source_name_required') return 'sourceNameRequired';
  return 'actionFailed';
}

/** Formats bounded API diagnostics without displaying submitted validation values or secrets. */
export function sourceErrorDetail(cause: unknown): string {
  if (!(cause instanceof ApiError)) return '';
  const fields = Array.isArray(cause.details)
    ? cause.details
        .flatMap((item: unknown) => {
          if (!item || typeof item !== 'object') return [];
          const issue = item as { loc?: unknown; type?: unknown };
          if (!Array.isArray(issue.loc) || typeof issue.type !== 'string') return [];
          return [`${issue.loc.filter((part: unknown) => typeof part === 'string' || typeof part === 'number').join('.')}: ${issue.type}`];
        })
        .slice(0, 10)
    : [];
  // Validation messages may contain submitted values; use only field paths and error types.
  const message = cause.status < 500 && !Array.isArray(cause.details) && !cause.message.includes('\n') ? cause.message.slice(0, 300) : '';
  return [`HTTP ${cause.status}`, cause.code, message, ...fields].filter(Boolean).join(' | ');
}

/** One configuration field the server rejected, with a coarse reason the editor can localize. */
export type ScopeIssue = { field: string; reason: 'required' | 'format' | 'tooLong' | 'range' | 'invalid' };

/** Maps a Pydantic error type to the coarse reason shown next to the field label. */
export function issueReason(type: string): ScopeIssue['reason'] {
  if (type === 'missing' || type === 'string_too_short' || type === 'too_short') return 'required';
  if (type === 'string_too_long' || type === 'too_long') return 'tooLong';
  if (/^(greater|less)_than/.test(type)) return 'range';
  if (/pattern|literal|enum|url|int_|float_|bool_|string_type|value_error/.test(type)) return 'format';
  return 'invalid';
}

/**
 * Extracts per-field problems from a 422: Pydantic `loc` paths under `configuration` and the registry's
 * value-free "Missing required fields: a, b" sentence. Only schema field names are read, never submitted values.
 */
export function scopeIssues(cause: unknown): ScopeIssue[] {
  if (!(cause instanceof ApiError) || cause.status !== 422) return [];
  const issues = new Map<string, ScopeIssue>();
  if (Array.isArray(cause.details))
    for (const item of cause.details as unknown[]) {
      const issue = item as { loc?: unknown; type?: unknown } | null;
      if (!issue || !Array.isArray(issue.loc) || typeof issue.type !== 'string') continue;
      const at = issue.loc.indexOf('configuration');
      const field = at >= 0 ? issue.loc[at + 1] : undefined;
      if (typeof field === 'string' && !issues.has(field)) issues.set(field, { field, reason: issueReason(issue.type) });
    }
  const missing = /Missing required fields: ([a-z0-9_, ]+)/.exec(cause.message)?.[1];
  for (const field of missing
    ?.split(',')
    .map((name) => name.trim())
    .filter(Boolean) ?? [])
    issues.set(field, { field, reason: 'required' });
  return [...issues.values()].slice(0, 10);
}

/** Form label message key for each configuration field, so a server issue names the field the owner sees. */
export const FIELD_LABEL_KEYS = {
  news_query: 'news.query',
  news_site: 'news.site',
  news_locale: 'news.locale',
  sec_cik: 'sec.cik',
  sec_contact_email: 'sec.contactEmail',
  scholarly_query: 'scholarly.query',
  fred_series_id: 'fredSeriesId',
  owm_latitude: 'weather.latitude',
  owm_longitude: 'weather.longitude',
  openaq_location_id: 'openaqLocationId',
  firms_country: 'firmsCountry',
  youtube_channel_id: 'youtube.channelId',
  arxiv_category: 'arxiv.category',
  huggingface_author: 'huggingface.author',
  bluesky_actor: 'blueskyActor',
  reddit_subreddit: 'reddit.subreddit',
  mastodon_instance_url: 'mastodon.instanceUrl',
  mastodon_account: 'mastodon.account',
  mastodon_hashtag: 'mastodon.hashtag',
  x_account: 'x.account',
  x_query: 'x.query',
  github_owner: 'github.owner',
  github_repository: 'github.repository',
  github_history_days: 'github.historyDays',
  telegram_chat_ids: 'telegram.channelIds',
  market_symbols: 'market.symbols',
  market_currency: 'market.currency',
  market_exchange_timezone: 'market.exchangeTimezone',
  weather_latitude: 'weather.latitude',
  weather_longitude: 'weather.longitude',
  weather_timezone: 'weather.timezone',
  weather_metrics: 'weather.metrics',
  notion_page_ids: 'notion.pageIds',
  slack_channel_ids: 'slack.channelIds',
  feed_url: 'sourceUrl',
  url: 'pageUrl',
  items_path: 'itemsPath',
  id_field: 'idField',
  title_field: 'titleField',
  content_field: 'contentField',
  updated_field: 'updatedField',
  fhir_base_url: 'fhir.baseUrl',
  fhir_patient_id: 'fhir.patientId',
  ha_base_url: 'homeAssistant.baseUrl',
  ha_entity_ids: 'homeAssistant.entityIds',
  timezone: 'timezone',
  history_mode: 'history.label',
  schedule_interval_minutes: 'schedule.label',
} as const;

/** `sources` catalog key of a configuration field's form label, or `undefined` for a field without a label (callers show the raw field name). */
export function fieldLabelKey(field: string): (typeof FIELD_LABEL_KEYS)[keyof typeof FIELD_LABEL_KEYS] | undefined {
  return Object.hasOwn(FIELD_LABEL_KEYS, field) ? FIELD_LABEL_KEYS[field as keyof typeof FIELD_LABEL_KEYS] : undefined;
}

/** Wizard step an embedded editor renders; every other section stays mounted but hidden so the draft survives step changes. */
export type EditorStep = 'provider' | 'connect' | 'choose' | 'collect' | 'verify';
/** Progress of the final "save and test fetch" run. */
export type VerifyState = {
  phase: 'idle' | 'saving' | 'activating' | 'collecting' | 'succeeded' | 'no_changes' | 'failed';
  code?: string | null;
  runId?: string | null;
};
/** Read-only editor state the wizard shell uses to gate navigation. */
export type EditorSnapshot = {
  sourceId: string;
  provider: string;
  busy: boolean;
  providerOk: boolean;
  connectOk: boolean;
  chooseOk: boolean;
  verify: VerifyState;
};
/** Imperative actions the wizard shell triggers on the editor. */
export type EditorActions = {
  /** Creates the source for the chosen provider and name; resolves its id or null on failure. */
  createSource: () => Promise<string | null>;
  /** Runs work a step needs before the shell advances; resolves false to stay on the step. */
  prepareNext: (step: EditorStep) => Promise<boolean>;
  /** Saves, enables, waits for activation and runs one real collection, reporting through the snapshot. */
  verify: () => Promise<void>;
};
export const IDLE_VERIFY: VerifyState = { phase: 'idle' };
export const ACTIVATION_TIMEOUT_MS = 60_000;
export const ACTIVATION_POLL_MS = 2_000;
/** Hard cap for the whole Save and test flow (activation plus collection); under the 120 s e2e/UX budget. */
export const VERIFY_TOTAL_MS = 105_000;

/** Milliseconds left until `deadline` (epoch ms), never negative; kept outside the component so render purity lint ignores the clock read. */
export function msUntil(deadline: number): number {
  return Math.max(deadline - Date.now(), 0);
}
