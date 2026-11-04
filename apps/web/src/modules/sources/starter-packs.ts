import { clampInterval, type BuiltinTypeOptions, type ConnectorConfig, type ConnectorCatalogEntry, type ScopedSourceSpec } from './api';

/** Message-key suffixes of the in-row editable value a pack item exposes (kept to the few values owners really change). */
export type ItemEdit = 'vn_symbols' | 'job_keywords' | 'crypto_symbols' | 'github_repo' | 'arxiv_category' | 'weather';

/** Built-in options a starter item may send; names mirror `BuiltinEnableRequest`. */
export type BuiltinItemOptions = BuiltinTypeOptions & { feed_sections?: string[] };

export type BuiltinPackItem = { id: string; kind: 'builtin'; provider: string; options?: BuiltinItemOptions; edit?: ItemEdit };
export type ScopedPackItem = {
  id: string; kind: 'scoped'; provider: string; sourceType: ScopedSourceSpec['type'];
  /** Message key (`starterName_*`) for the default source name. */
  nameKey: 'starterName_arxiv' | 'starterName_github_releases' | 'starterName_open_meteo' | 'starterName_google_news';
  configuration: Partial<ConnectorConfig>;
  /** Preferred interval; raised to the provider floor from the catalog when needed. */
  intervalMinutes: ConnectorConfig['schedule_interval_minutes'];
  edit?: ItemEdit;
};
export type PackItem = BuiltinPackItem | ScopedPackItem;
export type StarterPackId = 'developer' | 'jobs' | 'crypto' | 'finance' | 'weather' | 'news';
export type StarterPack = { id: StarterPackId; labelKey: `starterPack${'Developer' | 'Jobs' | 'Crypto' | 'Finance' | 'Weather' | 'News'}`; items: PackItem[]; guide?: 'linkedin' };

/**
 * The shipped starter packs (all keyless). Adding a provider to a pack is one entry; built-ins the server does not
 * list are hidden by the wizard, so entries for providers that are not deployed yet are harmless.
 */
export const STARTER_PACKS: StarterPack[] = [
  { id: 'developer', labelKey: 'starterPackDeveloper', items: [
    { id: 'hn_top', kind: 'builtin', provider: 'hn_top' },
    { id: 'lobsters', kind: 'builtin', provider: 'lobsters' },
    { id: 'github_trending', kind: 'builtin', provider: 'github_trending', options: { feed_sections: ['daily-all', 'weekly-typescript', 'weekly-python'] } },
    { id: 'arxiv', kind: 'scoped', provider: 'arxiv', sourceType: 'api', nameKey: 'starterName_arxiv', configuration: { arxiv_category: 'cs.AI' }, intervalMinutes: 360, edit: 'arxiv_category' },
    { id: 'github_releases', kind: 'scoped', provider: 'github_releases', sourceType: 'api', nameKey: 'starterName_github_releases', configuration: { github_owner: 'vercel', github_repository: 'next.js' }, intervalMinutes: 60, edit: 'github_repo' },
    { id: 'nvd', kind: 'builtin', provider: 'nvd' },
    { id: 'cisa_kev', kind: 'builtin', provider: 'cisa_kev' },
  ] },
  { id: 'jobs', labelKey: 'starterPackJobs', guide: 'linkedin', items: [
    { id: 'vietnamworks_jobs', kind: 'builtin', provider: 'vietnamworks_jobs', options: { job_keywords: ['developer'], job_cities: ['ho_chi_minh'] }, edit: 'job_keywords' },
    { id: 'itviec_jobs', kind: 'builtin', provider: 'itviec_jobs', options: { job_keywords: ['developer'], job_cities: ['ho_chi_minh'] } },
    { id: 'wwr_jobs', kind: 'builtin', provider: 'wwr_jobs', options: { feed_sections: ['programming'] } },
  ] },
  { id: 'crypto', labelKey: 'starterPackCrypto', items: [
    { id: 'binance', kind: 'builtin', provider: 'binance', options: { crypto_symbols: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT'] }, edit: 'crypto_symbols' },
    { id: 'coinpaprika', kind: 'builtin', provider: 'coinpaprika' },
    { id: 'alternative_me', kind: 'builtin', provider: 'alternative_me' },
  ] },
  { id: 'finance', labelKey: 'starterPackFinance', items: [
    { id: 'vn_stock', kind: 'builtin', provider: 'vn_stock', options: { vn_symbols: ['VNINDEX', 'VN30', 'FPT', 'VCB', 'HPG', 'VNM', 'MWG'] }, edit: 'vn_symbols' },
    { id: 'vnexpress_business', kind: 'builtin', provider: 'vnexpress_business' },
    { id: 'google_news', kind: 'scoped', provider: 'google_news', sourceType: 'rss', nameKey: 'starterName_google_news', intervalMinutes: 30,
      configuration: { news_query: 'VN-Index chứng khoán', news_site: 'any', news_locale: 'vi-VN' } },
    { id: 'frankfurter', kind: 'builtin', provider: 'frankfurter', options: { fx_base: 'USD', fx_quotes: ['VND'] } },
    { id: 'fed_press', kind: 'builtin', provider: 'fed_press' },
    { id: 'world_bank', kind: 'builtin', provider: 'world_bank', options: { wb_country: 'VN' } },
  ] },
  { id: 'weather', labelKey: 'starterPackWeather', items: [
    { id: 'open_meteo', kind: 'scoped', provider: 'open_meteo', sourceType: 'api', nameKey: 'starterName_open_meteo', intervalMinutes: 60, edit: 'weather',
      configuration: { weather_metrics: ['temperature_2m'], weather_timezone: 'Asia/Ho_Chi_Minh', weather_latitude: 10.8231, weather_longitude: 106.6297 } },
  ] },
  { id: 'news', labelKey: 'starterPackNews', items: [
    { id: 'tuoitre_news', kind: 'builtin', provider: 'tuoitre_news' },
    { id: 'bbc_world', kind: 'builtin', provider: 'bbc_world' },
  ] },
];

/** Splits comma/space separated codes into unique, upper-case terms. */
function codes(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map((term) => term.trim().toUpperCase()).filter(Boolean))];
}

/** Splits comma-separated search keywords (spaces allowed inside a keyword), trimmed and de-duplicated case-insensitively. */
function keywords(text: string): string[] {
  const seen = new Map<string, string>();
  for (const term of text.split(',').map((part) => part.trim()).filter(Boolean)) if (!seen.has(term.toLowerCase())) seen.set(term.toLowerCase(), term);
  return [...seen.values()];
}

/** Initial editable text for an item, derived from its default options or scope (empty when it has no editable value). */
export function defaultEditText(item: PackItem): string {
  if (item.kind === 'builtin' && item.edit === 'job_keywords') return item.options?.job_keywords?.join(', ') ?? '';
  if (item.kind === 'builtin') return (item.edit === 'vn_symbols' ? item.options?.vn_symbols : item.edit === 'crypto_symbols' ? item.options?.crypto_symbols : undefined)?.join(', ') ?? '';
  const c = item.configuration;
  if (item.edit === 'github_repo') return `${c.github_owner}/${c.github_repository}`;
  if (item.edit === 'arxiv_category') return c.arxiv_category ?? '';
  if (item.edit === 'weather') return `${c.weather_latitude}, ${c.weather_longitude}`;
  return '';
}

/** Whether the edited text is acceptable for the item; the server stays the final validator. */
export function editTextValid(item: PackItem, text: string): boolean {
  switch (item.edit) {
    case 'vn_symbols': { const list = codes(text); return list.length >= 1 && list.length <= 20 && list.every((code) => /^[A-Z0-9]{2,10}$/.test(code)); }
    case 'job_keywords': { const list = keywords(text); return list.length >= 1 && list.length <= 5 && list.every((term) => term.length <= 60); }
    case 'crypto_symbols': { const list = codes(text); return list.length >= 1 && list.length <= 10 && list.every((code) => /^[A-Z0-9]{3,20}$/.test(code)); }
    case 'github_repo': return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(text.trim());
    case 'arxiv_category': return /^[a-z-]+(\.[A-Za-z-]+)?$/.test(text.trim());
    case 'weather': { const [lat, lon] = text.split(',').map((part) => Number(part.trim())); return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 && text.split(',').length === 2; }
    default: return true;
  }
}

/** Built-in enable options for an item after applying its edited text. */
export function builtinOptionsFor(item: BuiltinPackItem, text: string): BuiltinItemOptions {
  const options: BuiltinItemOptions = { ...item.options };
  if (item.edit === 'vn_symbols') options.vn_symbols = codes(text);
  if (item.edit === 'crypto_symbols') options.crypto_symbols = codes(text);
  if (item.edit === 'job_keywords') options.job_keywords = keywords(text);
  return options;
}

/** Scoped-source creation spec for an item with its edited text, display name and the catalog entry for the interval floor. */
export function scopedSpecFor(item: ScopedPackItem, text: string, name: string, entry: ConnectorCatalogEntry | undefined): ScopedSourceSpec {
  const configuration: ConnectorConfig = {
    timeout_seconds: 30, timezone: 'Asia/Ho_Chi_Minh', history_mode: 'returned_snapshot',
    ...item.configuration, schedule_interval_minutes: clampInterval(item.intervalMinutes, entry),
  };
  if (item.edit === 'github_repo') { const [owner, repo] = text.trim().split('/'); configuration.github_owner = owner; configuration.github_repository = repo; }
  if (item.edit === 'arxiv_category') configuration.arxiv_category = text.trim();
  if (item.edit === 'weather') { const [lat, lon] = text.split(',').map((part) => Number(part.trim())); configuration.weather_latitude = lat; configuration.weather_longitude = lon; }
  return { provider: item.provider, type: item.sourceType, name, configuration, termsVersion: entry?.kind === 'preset' ? entry.terms_checked_on ?? undefined : undefined };
}
