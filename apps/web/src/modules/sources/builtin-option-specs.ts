import type { BuiltinTypeOptions } from './api';

/** One per-type built-in option: how it renders and the bounds the server enforces. */
export type OptionSpec = {
  key: keyof BuiltinTypeOptions;
  kind: 'enum' | 'multi' | 'int' | 'float' | 'bool' | 'text' | 'tags';
  /** Enum / multi-enum values, identical to the server Literal (modules/connectors/provider_options.py). */
  values?: readonly string[];
  /** Multi-select cap, or the most comma-separated terms a `tags` field accepts. */
  max?: number;
  min?: number;
  /** Numeric ceiling (inclusive). */
  hi?: number;
  step?: number;
  maxLen?: number;
  /** Changes the upstream request, so a change restarts collection from the recent window. */
  native?: boolean;
  /** Text is forced to upper case (ISO codes). */
  upper?: boolean;
  /** Checkbox values have translated labels under `optv_<value>` (otherwise the raw code is shown). */
  labelled?: boolean;
};

const GDELT_COUNTRY = ['US', 'VN', 'CN', 'JP', 'GB', 'DE', 'FR', 'IN', 'KR', 'SG', 'AU', 'RU', 'BR', 'CA'] as const;
const REGION = ['world', 'southeast_asia', 'vietnam', 'east_asia', 'south_asia', 'europe', 'north_america', 'south_america', 'africa', 'oceania', 'middle_east'] as const;
const ISO_COUNTRY = ['VN', 'US', 'JP', 'CN', 'GB', 'DE', 'FR', 'IN', 'KR', 'SG', 'AU', 'CA', 'BR', 'ID', 'TH', 'MY', 'PH', 'IT', 'ES', 'RU', 'MX', 'ZA', 'TR', 'NL', 'CH'] as const;
const FX_CURRENCY = [
  'EUR', 'USD', 'JPY', 'GBP', 'CNY', 'VND', 'KRW', 'SGD', 'AUD', 'CAD', 'THB', 'INR', 'CHF', 'HKD', 'IDR', 'MYR',
  'PHP', 'BGN', 'CZK', 'DKK', 'HUF', 'PLN', 'RON', 'SEK', 'ISK', 'NOK', 'TRY', 'BRL', 'ILS', 'MXN', 'NZD', 'ZAR',
] as const;
const NWS_AREA = [
  'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'DC', 'FL', 'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY',
  'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH',
  'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY',
] as const;

/**
 * Every per-type option the Options step can render, in display order. The component shows only the keys the server
 * lists in `applicable_options`. Values mirror the backend enums; the server stays the validator (422 on mismatch).
 */
export const OPTION_SPECS: readonly OptionSpec[] = [
  { key: 'hn_list', kind: 'enum', values: ['top', 'new', 'best', 'ask', 'show'] },
  { key: 'gdelt_query', kind: 'text', maxLen: 100, native: true },
  { key: 'gdelt_timespan', kind: 'enum', values: ['1h', '6h', '24h', '3d', '7d'] },
  { key: 'gdelt_source_lang', kind: 'enum', native: true, values: ['english', 'vietnamese', 'chinese', 'japanese', 'korean', 'french', 'german', 'spanish'] },
  { key: 'gdelt_source_country', kind: 'enum', native: true, values: GDELT_COUNTRY },
  { key: 'usgs_min_magnitude', kind: 'enum', values: ['significant', '4.5', '2.5', '1.0', 'all'] },
  { key: 'usgs_period', kind: 'enum', values: ['hour', 'day', 'week'] },
  { key: 'min_magnitude', kind: 'float', min: 2, hi: 8, step: 0.1, native: true },
  { key: 'eonet_categories', kind: 'multi', max: 13, values: ['drought', 'dustHaze', 'earthquakes', 'floods', 'landslides', 'manmade', 'seaLakeIce', 'severeStorms', 'snow', 'tempExtremes', 'volcanoes', 'waterColor', 'wildfires'] },
  { key: 'eonet_status', kind: 'enum', values: ['open', 'all'] },
  { key: 'eonet_days', kind: 'int', min: 1, hi: 30 },
  { key: 'gdacs_event_types', kind: 'multi', max: 6, native: true, values: ['EQ', 'TC', 'FL', 'VO', 'WF', 'DR'] },
  { key: 'gdacs_alert_levels', kind: 'multi', max: 3, native: true, values: ['Green', 'Orange', 'Red'] },
  { key: 'nws_severity', kind: 'multi', max: 4, native: true, values: ['Extreme', 'Severe', 'Moderate', 'Minor'] },
  { key: 'nws_area', kind: 'enum', native: true, values: NWS_AREA },
  { key: 'nvd_severity', kind: 'multi', max: 4, values: ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'] },
  { key: 'nvd_window_days', kind: 'int', min: 1, hi: 7 },
  { key: 'nvd_keyword', kind: 'text', maxLen: 100 },
  { key: 'kev_ransomware_only', kind: 'bool' },
  { key: 'ioda_country', kind: 'text', maxLen: 2, upper: true },
  { key: 'region', kind: 'enum', values: REGION },
  { key: 'wiki_language', kind: 'enum', values: ['en', 'vi', 'ja', 'zh', 'fr', 'de', 'es', 'ko'] },
  { key: 'satnogs_status', kind: 'enum', native: true, values: ['good', 'bad', 'unknown'] },
  { key: 'disease_country', kind: 'enum', values: ISO_COUNTRY },
  { key: 'wb_country', kind: 'enum', values: ISO_COUNTRY },
  { key: 'wb_indicator', kind: 'enum', values: ['gdp', 'gdp_growth', 'inflation_cpi', 'unemployment', 'population', 'fdi'] },
  { key: 'fx_base', kind: 'enum', values: FX_CURRENCY },
  { key: 'fx_quotes', kind: 'multi', max: 10, values: FX_CURRENCY },
  { key: 'oecd_countries', kind: 'multi', max: 10, values: ['USA', 'CHN', 'JPN', 'DEU', 'GBR', 'FRA', 'ITA', 'CAN', 'KOR', 'IND', 'BRA', 'AUS', 'ESP', 'MEX', 'TUR', 'IDN'] },
  { key: 'crypto_symbols', kind: 'multi', max: 10, values: ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT', 'TRXUSDT', 'LINKUSDT', 'DOTUSDT', 'LTCUSDT', 'AVAXUSDT'] },
  // Free-form tickers (e.g. FPT, VNINDEX): comma-separated, forced upper case; the server validates the 2-10 char code pattern.
  { key: 'vn_symbols', kind: 'tags', max: 20, maxLen: 10, upper: true },
  // Job search filters: free-form keywords (server caps 5 x 60 chars), city/level checkbox groups and a USD salary floor.
  { key: 'job_keywords', kind: 'tags', max: 5, maxLen: 60 },
  { key: 'job_cities', kind: 'multi', labelled: true, max: 3, values: ['ho_chi_minh', 'ha_noi', 'da_nang'] },
  { key: 'job_levels', kind: 'multi', labelled: true, max: 5, values: ['intern', 'fresher', 'experienced', 'manager', 'director'] },
  { key: 'job_min_salary_usd', kind: 'int', min: 0, hi: 20000 },
  { key: 'coin_ids', kind: 'multi', max: 5, values: ['btc-bitcoin', 'eth-ethereum', 'bnb-binance-coin', 'sol-solana', 'xrp-xrp', 'ada-cardano', 'doge-dogecoin', 'ltc-litecoin', 'dot-polkadot', 'trx-tron'] },
  { key: 'kp_min', kind: 'float', min: 0, hi: 9, step: 0.1 },
  { key: 'advisory_min_level', kind: 'int', min: 1, hi: 4 },
];
