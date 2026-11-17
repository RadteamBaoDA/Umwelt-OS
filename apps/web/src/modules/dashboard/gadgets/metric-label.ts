import type { useTranslations } from 'next-intl';

/** The `dashboard` namespace translator the label helpers read from. */
type LabelTranslator = ReturnType<typeof useTranslations<'dashboard'>>;

/** Literal `dashboard` catalog keys per provider metric id (no runtime key concatenation); unknown ids use sentence case. */
const METRIC_LABEL_KEY = {
  close: 'metric.close',
  open: 'metric.open',
  high: 'metric.high',
  low: 'metric.low',
  volume: 'metric.volume',
  spot_price: 'metric.spotPrice',
  price: 'metric.price',
  fear_greed_index: 'metric.fearGreedIndex',
  fx_reference_rate: 'metric.fxReferenceRate',
  composite_leading_indicator: 'metric.compositeLeadingIndicator',
  gdp_current_usd: 'metric.gdpCurrentUsd',
  fred_series: 'metric.fredSeries',
  kp_index: 'metric.kpIndex',
  iss_altitude: 'metric.issAltitude',
  earthquake_magnitude: 'metric.earthquakeMagnitude',
  fire_radiative_power: 'metric.fireRadiativePower',
  covid19_cases: 'metric.covid19Cases',
  covid19_deaths: 'metric.covid19Deaths',
  covid19_recovered: 'metric.covid19Recovered',
  covid19_active: 'metric.covid19Active',
  temperature: 'metric.temperature',
  temperature_2m: 'metric.temperature2m',
  humidity: 'metric.humidity',
  relative_humidity_2m: 'metric.relativeHumidity2m',
  pressure: 'metric.pressure',
  wind_speed: 'metric.windSpeed',
  wind_speed_10m: 'metric.windSpeed10m',
  precipitation: 'metric.precipitation',
  pm25: 'metric.pm25',
  pm10: 'metric.pm10',
  o3: 'metric.o3',
  no2: 'metric.no2',
  so2: 'metric.so2',
  co: 'metric.co',
} as const;

/** Literal `dashboard` catalog keys per backend reason code; unknown codes use `reason.unknown`. */
const REASON_LABEL_KEY = {
  method_data_license_unverified: 'reason.methodDataLicenseUnverified',
  co_occurrence_is_not_causation: 'reason.coOccurrenceIsNotCausation',
  insufficient_domain_evidence: 'reason.insufficientDomainEvidence',
  bounded_source_coverage: 'reason.boundedSourceCoverage',
  bounded_evidence_support: 'reason.boundedEvidenceSupport',
  unknown: 'reason.unknown',
} as const;

/** "temperature_2m" -> "Temperature 2m": replace underscores and capitalise the first letter. */
function sentenceCase(id: string): string {
  const spaced = id.replace(/_/g, ' ').trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/**
 * Human label for a provider metric id (e.g. `fear_greed_index`).
 * Known ids resolve through {@link METRIC_LABEL_KEY}; anything else falls back to sentence case
 * so a raw snake_case identifier is never shown. Pure; no side effects.
 */
export function formatMetricLabel(id: string, t: LabelTranslator): string {
  return id in METRIC_LABEL_KEY ? t(METRIC_LABEL_KEY[id as keyof typeof METRIC_LABEL_KEY]) : sentenceCase(id);
}

/**
 * Human sentence for a backend reason/availability code (e.g. `method_data_license_unverified`).
 * Known codes resolve through {@link REASON_LABEL_KEY}; unknown codes use the generic `reason.unknown` sentence.
 */
export function formatReasonLabel(code: string, t: LabelTranslator): string {
  return t(code in REASON_LABEL_KEY ? REASON_LABEL_KEY[code as keyof typeof REASON_LABEL_KEY] : 'reason.unknown');
}

const PROVIDER_NAMES: Record<string, string> = {
  binance: 'Binance',
  coingecko: 'CoinGecko',
  alpha_vantage: 'Alpha Vantage',
  frankfurter: 'Frankfurter',
  yahoo: 'Yahoo Finance',
  open_meteo: 'Open-Meteo',
};

/**
 * Display name for a provider id: known providers use their brand spelling, anything else is humanized
 * (`some_provider` -> "Some provider") so a raw id is never shown. Pure.
 */
export function formatProviderName(id: string): string {
  return PROVIDER_NAMES[id] ?? sentenceCase(id);
}
