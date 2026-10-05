import { apiRequest, csrfHeaders } from '@/core/api';

/** Dashboard summary returned by the owner-scoped collection endpoint. */
export type DashboardSummary = {
  id: string;
  name: string;
  revision: number;
  created_at: string;
  updated_at: string;
};

/** Persisted rectangle for one gadget instance in a single breakpoint layout. */
export type DashboardPlacement = {
  instance_id: string;
  x: number;
  y: number;
  w: number;
  h: number;
};

/** Complete saved layout for one breakpoint, including its independent column count. */
export type DashboardBreakpointLayout = { columns: number; items: DashboardPlacement[] };

/** Warning metadata describing planned rendering or unavailable source/capability state. */
export type DashboardWarning = {
  code: string;
  capability: string | null;
  source_id: string | null;
  setup_group: string | null;
};

/** Reusable saved renderer configuration embedded in dashboard detail responses. */
export type GadgetDefinition = {
  id: string;
  name: string;
  revision: number;
  renderer: string;
  source_ids: string[];
  scope: GadgetScope;
  filters: GadgetFilters;
  highlight_rules: HighlightRule[];
  config_version: number;
  runtime_state: 'planned' | 'available';
  warnings?: DashboardWarning[];
};

/** Bounded source, channel, symbol, region, and map-layer selectors stored as configuration. */
export type GadgetScope = {
  source_item_ids?: string[];
  channel_ids?: string[];
  symbols?: string[];
  regions?: string[];
  map_layer_ids?: string[];
  metrics?: string[];
  lookback_days?: number;
};

/** Explainable match against one immutable current document version. */
export type DashboardHighlightMatch = {
  document_id: string; document_version_id: string; source_id: string; title: string;
  observed_at: string; rule_id: string; matched_keywords: string[];
  severity: 'info' | 'warning' | 'critical'; notify: boolean; reason: string;
};

/** Simple bounded text filters and strict integer item limit. */
export type GadgetFilters = {
  keywords?: string[];
  exclude_keywords?: string[];
  limit?: number;
};

/** Non-executable highlight configuration; it does not imply notification execution. */
export type HighlightRule = {
  id: string;
  keywords: string[];
  severity: 'info' | 'warning' | 'critical';
  notify: boolean;
};

/** Dashboard gadget placement reference and the associated saved definition projection. */
export type GadgetInstance = {
  id: string;
  group_id: string;
  definition_id: string;
  title: string | null;
  position: number;
  definition: GadgetDefinition;
};

/** Ordered dashboard group as returned in detail and group collection responses. */
export type DashboardGroup = {
  id: string;
  dashboard_id: string;
  name: string;
  position: number;
};

/** Full owner-scoped dashboard configuration, without rendered payload data. */
export type Dashboard = DashboardSummary & {
  groups: DashboardGroup[];
  instances: GadgetInstance[];
  layouts: { desktop: DashboardBreakpointLayout; mobile: DashboardBreakpointLayout };
};

/** Metadata-only source selection row; source content and connector settings are excluded. */
export type GadgetSource = {
  id: string;
  name: string;
  type: string;
  provider: string | null;
  status: 'active' | 'paused' | 'archived';
  generation: number;
  local_only: boolean;
};

/** Cursor page for bounded source selection. */
export type GadgetSourcePage = { items: GadgetSource[]; next_cursor: string | null };

/** Static renderer metadata; available means an adapter exists, not runtime/provider acceptance. */
export type GadgetRenderer = {
  id: string;
  config_version: 1;
  minimum_width: number;
  minimum_height: number;
  runtime_state: 'planned' | 'available';
  capability_keys: string[];
};

/** Static dashboard preset catalog entry with stable renderer slot identities. */
export type DashboardPreset = {
  id: string;
  label: string;
  family: string;
  slots: { slot_id: string; renderer: string }[];
};

/** Input selecting explicit source IDs for known preset slots and an optional target. */
export type PresetPreviewRequest = {
  slot_sources?: Record<string, string[]>;
  target_dashboard_id?: string | null;
};

/** Fully resolved, non-persistent preview returned before an explicit apply request. */
export type PresetPreview = {
  template_version: number;
  preset_id: string;
  name: string;
  slots: {
    slot_id: string;
    renderer: string;
    source_ids: string[];
    scope: GadgetScope;
    filters: GadgetFilters;
    highlight_rules: HighlightRule[];
    sources: { id: string; generation: number | null; status: string }[];
    warnings: DashboardWarning[];
  }[];
  target_dashboard_id: string | null;
  target_revision: number | null;
  layouts: {
    desktop: { columns: number; items: { slot_id: string; x: number; y: number; w: number; h: number }[] };
    mobile: { columns: number; items: { slot_id: string; x: number; y: number; w: number; h: number }[] };
  };
  preview_fingerprint: string;
};

/** Write payload for creating a reusable gadget definition. */
export type GadgetDefinitionCreate = {
  name: string;
  renderer: string;
  source_ids?: string[];
  scope?: GadgetScope;
  filters?: GadgetFilters;
  highlight_rules?: HighlightRule[];
};

/** Revision-guarded partial configuration update for a reusable definition. */
export type GadgetDefinitionPatch = Partial<Omit<GadgetDefinitionCreate, 'name'>> & {
  expected_revision: number;
  name?: string;
};

/** Query key families invalidated by dashboard and definition SSE changes. */
export const dashboardKeys = {
  all: ['dashboards'] as const,
  /** Builds the identifier-specific dashboard detail key used by reads and SSE invalidation. */
  detail: (id: string) => ['dashboard', id] as const,
  definitions: ['gadget-definitions'] as const,
  /** Builds the identifier-specific definition key within the library invalidation family. */
  definition: (id: string) => ['gadget-definition', id] as const,
  renderers: ['gadget-renderers'] as const,
  sources: ['gadget-sources'] as const,
  presets: ['dashboard-presets'] as const,
  presetPreview: ['dashboard-preset-preview'] as const,
};

/** Lists the authenticated owner's dashboards in backend creation order. */
export function listDashboards(signal?: AbortSignal) {
  return apiRequest<DashboardSummary[]>('/api/v1/dashboards', { signal });
}

/** Reads a dashboard's groups, instances, configurations, lifecycle warnings, and both layouts. */
export function getDashboard(id: string, signal?: AbortSignal) {
  return apiRequest<Dashboard>(`/api/v1/dashboards/${encodeURIComponent(id)}`, { signal });
}

/** Creates an owner dashboard with empty desktop and mobile layouts. */
export function createDashboard(name: string, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<Dashboard>('/api/v1/dashboards', {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ name }), signal,
  });
}

/** Renames a dashboard only if its shared revision still matches. */
export function renameDashboard(id: string, name: string, expectedRevision: number, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<Dashboard>(`/api/v1/dashboards/${encodeURIComponent(id)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ name, expected_revision: expectedRevision }), signal,
  });
}

/** Deletes a dashboard at the supplied revision and propagates backend conflicts unchanged. */
export function deleteDashboard(id: string, expectedRevision: number, csrfToken: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ expected_revision: String(expectedRevision) });
  return apiRequest<void>(`/api/v1/dashboards/${encodeURIComponent(id)}?${query}`, {
    method: 'DELETE', headers: csrfHeaders(csrfToken), signal,
  });
}

/** Lists ordered groups belonging to one owner dashboard. */
export function listDashboardGroups(dashboardId: string, signal?: AbortSignal) {
  return apiRequest<DashboardGroup[]>(`/api/v1/dashboards/${encodeURIComponent(dashboardId)}/groups`, { signal });
}

/** Creates a dashboard group under the shared dashboard revision. */
export function createDashboardGroup(dashboardId: string, name: string, expectedRevision: number, csrfToken: string, position = 0, signal?: AbortSignal) {
  return apiRequest<DashboardGroup>(`/api/v1/dashboards/${encodeURIComponent(dashboardId)}/groups`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ name, expected_revision: expectedRevision, position }), signal,
  });
}

/** Updates group name or ordering at the current dashboard revision. */
export function patchDashboardGroup(dashboardId: string, groupId: string, expectedRevision: number, patch: { name?: string; position?: number }, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<DashboardGroup>(`/api/v1/dashboards/${encodeURIComponent(dashboardId)}/groups/${encodeURIComponent(groupId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ ...patch, expected_revision: expectedRevision }), signal,
  });
}

/** Deletes an empty group under the dashboard revision guard. */
export function deleteDashboardGroup(dashboardId: string, groupId: string, expectedRevision: number, csrfToken: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ expected_revision: String(expectedRevision) });
  return apiRequest<void>(`/api/v1/dashboards/${encodeURIComponent(dashboardId)}/groups/${encodeURIComponent(groupId)}?${query}`, {
    method: 'DELETE', headers: csrfHeaders(csrfToken), signal,
  });
}

/** Adds a definition-backed instance and returns the committed dashboard projection. */
export function createGadgetInstance(dashboardId: string, payload: { expected_revision: number; group_id: string; definition_id: string; title?: string | null; position?: number }, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<Dashboard>(`/api/v1/dashboards/${encodeURIComponent(dashboardId)}/instances`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload), signal,
  });
}

/** Updates an instance's group, title, or order without changing its saved rectangles. */
export function patchGadgetInstance(dashboardId: string, instanceId: string, expectedRevision: number, patch: { group_id?: string; title?: string | null; position?: number }, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<Dashboard>(`/api/v1/dashboards/${encodeURIComponent(dashboardId)}/instances/${encodeURIComponent(instanceId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ ...patch, expected_revision: expectedRevision }), signal,
  });
}

/** Removes an instance and both breakpoint placements while retaining its definition. */
export function deleteGadgetInstance(dashboardId: string, instanceId: string, expectedRevision: number, csrfToken: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ expected_revision: String(expectedRevision) });
  return apiRequest<Dashboard>(`/api/v1/dashboards/${encodeURIComponent(dashboardId)}/instances/${encodeURIComponent(instanceId)}?${query}`, {
    method: 'DELETE', headers: csrfHeaders(csrfToken), signal,
  });
}

/** Replaces exactly one breakpoint's complete placement set under dashboard revision control. */
export function replaceDashboardLayout(dashboardId: string, payload: { expected_revision: number; breakpoint: 'desktop' | 'mobile'; columns?: number; items: DashboardPlacement[] }, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<Dashboard>(`/api/v1/dashboards/${encodeURIComponent(dashboardId)}/layout`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload), signal,
  });
}

/** Lists a bounded page of reusable owner definitions. */
export function listGadgetDefinitions(limit = 200, signal?: AbortSignal) {
  return apiRequest<GadgetDefinition[]>(`/api/v1/gadget-definitions?${new URLSearchParams({ limit: String(limit) })}`, { signal });
}

/** Reads one reusable definition and its saved configuration. */
export function getGadgetDefinition(id: string, signal?: AbortSignal) {
  return apiRequest<GadgetDefinition>(`/api/v1/gadget-definitions/${encodeURIComponent(id)}`, { signal });
}

/** Creates a reusable definition using the explicit owner CSRF token. */
export function createGadgetDefinition(payload: GadgetDefinitionCreate, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<GadgetDefinition>('/api/v1/gadget-definitions', {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload), signal,
  });
}

/** Applies a partial reusable definition update guarded by its independent revision. */
export function patchGadgetDefinition(id: string, payload: GadgetDefinitionPatch, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<GadgetDefinition>(`/api/v1/gadget-definitions/${encodeURIComponent(id)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload), signal,
  });
}

/** Deletes an unused definition only at its current revision. */
export function deleteGadgetDefinition(id: string, expectedRevision: number, csrfToken: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ expected_revision: String(expectedRevision) });
  return apiRequest<void>(`/api/v1/gadget-definitions/${encodeURIComponent(id)}?${query}`, {
    method: 'DELETE', headers: csrfHeaders(csrfToken), signal,
  });
}

/** Lists fixed renderer configuration metadata and minimum geometry, not runtime implementations. */
export function listGadgetRenderers(signal?: AbortSignal) {
  return apiRequest<GadgetRenderer[]>('/api/v1/gadget-renderers', { signal });
}

/** Lists bounded source identity and lifecycle metadata for explicit gadget selection. */
export function listGadgetSources(limit = 50, cursor?: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) query.set('cursor', cursor);
  return apiRequest<GadgetSourcePage>(`/api/v1/gadget-sources?${query}`, { signal });
}

/** Evaluates configured rules and returns bounded exact-version matches. */
export function evaluateGadgetHighlights(definitionId: string) {
  return apiRequest<DashboardHighlightMatch[]>(`/api/v1/gadget-definitions/${definitionId}/highlights`);
}

/** Lists the static preset catalog without applying or creating anything. */
export function listDashboardPresets(signal?: AbortSignal) {
  return apiRequest<DashboardPreset[]>('/api/v1/dashboard-presets', { signal });
}

/** Resolves a preset and explicit source selection without persistence; CSRF is required because the route is POST. */
export function previewDashboardPreset(presetId: string, payload: PresetPreviewRequest, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<PresetPreview>(`/api/v1/dashboard-presets/${encodeURIComponent(presetId)}/preview`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload), signal,
  });
}

/** Applies the fingerprinted preset; backend 409 ApiError details pass through unchanged. */
export function applyDashboardPreset(presetId: string, payload: PresetPreviewRequest & {
  preview_fingerprint: string;
  mode?: 'create' | 'replace';
  name?: string | null;
  expected_revision?: number | null;
  replace_confirmed?: boolean;
}, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<Dashboard>(`/api/v1/dashboard-presets/${encodeURIComponent(presetId)}/apply`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload), signal,
  });
}
