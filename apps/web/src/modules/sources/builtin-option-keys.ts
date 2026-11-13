/** Literal `sources` catalog key of each per-type built-in option's label, by option spec key (no runtime key building). */
export const OPTION_LABEL_KEY = {
  hn_list: 'optk.hnList',
  gdelt_query: 'optk.gdeltQuery',
  gdelt_timespan: 'optk.gdeltTimespan',
  gdelt_source_lang: 'optk.gdeltSourceLang',
  gdelt_source_country: 'optk.gdeltSourceCountry',
  usgs_min_magnitude: 'optk.usgsMinMagnitude',
  usgs_period: 'optk.usgsPeriod',
  min_magnitude: 'optk.minMagnitude',
  eonet_categories: 'optk.eonetCategories',
  eonet_status: 'optk.eonetStatus',
  eonet_days: 'optk.eonetDays',
  gdacs_event_types: 'optk.gdacsEventTypes',
  gdacs_alert_levels: 'optk.gdacsAlertLevels',
  nws_severity: 'optk.nwsSeverity',
  nws_area: 'optk.nwsArea',
  nvd_severity: 'optk.nvdSeverity',
  nvd_window_days: 'optk.nvdWindowDays',
  nvd_keyword: 'optk.nvdKeyword',
  kev_ransomware_only: 'optk.kevRansomwareOnly',
  ioda_country: 'optk.iodaCountry',
  region: 'optk.region',
  wiki_language: 'optk.wikiLanguage',
  satnogs_status: 'optk.satnogsStatus',
  disease_country: 'optk.diseaseCountry',
  wb_country: 'optk.wbCountry',
  wb_indicator: 'optk.wbIndicator',
  fx_base: 'optk.fxBase',
  fx_quotes: 'optk.fxQuotes',
  oecd_countries: 'optk.oecdCountries',
  crypto_symbols: 'optk.cryptoSymbols',
  coin_ids: 'optk.coinIds',
  kp_min: 'optk.kpMin',
  advisory_min_level: 'optk.advisoryMinLevel',
  vn_symbols: 'optk.vnSymbols',
  job_keywords: 'optk.jobKeywords',
  job_cities: 'optk.jobCities',
  job_levels: 'optk.jobLevels',
  job_min_salary_usd: 'optk.jobMinSalaryUsd',
} as const;

/** Literal `sources` catalog key of the label of each translated option value (`OptionSpec.labelled`), by value. */
export const OPTION_VALUE_KEY = {
  ho_chi_minh: 'optv.hoChiMinh',
  ha_noi: 'optv.haNoi',
  da_nang: 'optv.daNang',
  intern: 'optv.intern',
  fresher: 'optv.fresher',
  experienced: 'optv.experienced',
  manager: 'optv.manager',
  director: 'optv.director',
} as const;

/** Catalog key of a built-in option's label, or `undefined` for a spec key without a translation. */
export function optionLabelKey(specKey: string): (typeof OPTION_LABEL_KEY)[keyof typeof OPTION_LABEL_KEY] | undefined {
  return Object.hasOwn(OPTION_LABEL_KEY, specKey) ? OPTION_LABEL_KEY[specKey as keyof typeof OPTION_LABEL_KEY] : undefined;
}

/** Catalog key of a translated option value's label, or `undefined` when the value has no translation. */
export function optionValueKey(value: string): (typeof OPTION_VALUE_KEY)[keyof typeof OPTION_VALUE_KEY] | undefined {
  return Object.hasOwn(OPTION_VALUE_KEY, value) ? OPTION_VALUE_KEY[value as keyof typeof OPTION_VALUE_KEY] : undefined;
}
