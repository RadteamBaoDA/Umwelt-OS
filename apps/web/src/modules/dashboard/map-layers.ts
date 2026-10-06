import type { WorldObservation } from '@/modules/observations/api';

/** Supported client renderers that share one selected feature and layer controller. */
export type MapEngine = 'globe' | 'flat';

/** Stable detached map point tied to one current authorized observation and evidence version. */
export type MapFeature = {
  feature_id: string;
  source_id: string;
  observation_id: string;
  document_id: string;
  document_version_id: string;
  document_version_number: number | null;
  region: string | null;
  latitude: number;
  longitude: number;
  observed_at: string;
  metric: string;
  value: number | null;
  unit: string;
  quality: string;
  provider: string;
};

/** Describe one persisted map layer; unavailable domain layers carry a clear reason and no guessed points. */
export type MapLayer = {
  id: string;
  source_ids: string[];
  geometry_kind: 'point' | 'line' | 'polygon';
  domain_family: 'evidence' | 'military' | 'economic' | 'disaster' | 'escalation';
  enabled: boolean;
  attribution: string;
  attribution_url: string | null;
  license_status: 'source_specific' | 'unverified' | 'not_applicable';
  availability: 'available' | 'unavailable';
  reason: 'privacy_opt_in_required' | 'no_source_selected' | 'domain_geo_adapter_unavailable' | null;
  features: MapFeature[];
};

/** Keep the initial map catalog local and truthful; no browser tile or provider request is performed. */
const DOMAIN_LAYER_DEFINITIONS: ReadonlyArray<Pick<MapLayer,
  'id' | 'geometry_kind' | 'domain_family' | 'attribution' | 'attribution_url' | 'license_status' | 'reason'
>> = [
  { id: 'world_observations', geometry_kind: 'point', domain_family: 'evidence', attribution: 'Open-Meteo; source terms apply', attribution_url: 'https://open-meteo.com/', license_status: 'source_specific', reason: null },
  { id: 'military', geometry_kind: 'point', domain_family: 'military', attribution: '', attribution_url: null, license_status: 'unverified', reason: 'domain_geo_adapter_unavailable' },
  { id: 'economic', geometry_kind: 'point', domain_family: 'economic', attribution: '', attribution_url: null, license_status: 'unverified', reason: 'domain_geo_adapter_unavailable' },
  { id: 'disaster', geometry_kind: 'point', domain_family: 'disaster', attribution: '', attribution_url: null, license_status: 'unverified', reason: 'domain_geo_adapter_unavailable' },
  { id: 'escalation', geometry_kind: 'point', domain_family: 'escalation', attribution: '', attribution_url: null, license_status: 'unverified', reason: 'domain_geo_adapter_unavailable' },
];

/** Convert only provider-declared coordinates and exact evidence IDs into common engine features. */
export function projectMapFeatures(observations: WorldObservation[]): MapFeature[] {
  return observations
    .filter((item) => Number.isFinite(item.latitude) && Number.isFinite(item.longitude))
    .map((item) => ({
      feature_id: item.id,
      source_id: item.source_id,
      observation_id: item.id,
      document_id: item.document_id,
      document_version_id: item.document_version_id,
      document_version_number: item.document_version_number,
      region: item.region,
      latitude: item.latitude as number,
      longitude: item.longitude as number,
      observed_at: item.observed_at,
      metric: item.metric,
      value: item.value,
      unit: item.unit,
      quality: item.quality,
      provider: item.provider,
    }));
}

/** Build identical engine inputs from saved source/layer selection and one shared result page set. */
export function buildMapLayers(
  sourceIds: string[], selectedIds: string[], features: MapFeature[], showPreciseLocations: boolean,
): MapLayer[] {
  const enabledIds = new Set(selectedIds.length ? selectedIds : ['world_observations']);
  return DOMAIN_LAYER_DEFINITIONS.map((definition) => {
    const worldLayer = definition.id === 'world_observations';
    const reason = worldLayer
      ? !showPreciseLocations ? 'privacy_opt_in_required' : sourceIds.length === 0 ? 'no_source_selected' : null
      : definition.reason;
    return {
      ...definition,
      source_ids: worldLayer ? [...sourceIds] : [],
      enabled: enabledIds.has(definition.id),
      availability: reason === null ? 'available' : 'unavailable',
      reason,
      features: worldLayer && reason === null ? features : [],
    };
  });
}
