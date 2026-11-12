'use client';

import { useInfiniteQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import Link from 'next/link';
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listGeospatialObservations } from '@/modules/observations';
import type { GadgetInstance } from '../api';
import { buildMapLayers, projectMapFeatures } from '../map-layers';
import { GadgetNotice } from './gadget-notice';
import { LiveMapGadget } from './live-map-gadget';
import { formatMetricLabel, formatProviderName } from './metric-label';
import { BareButton } from '@/components/bare-button';

const WorldMap = lazy(() => import('./world-map').then((module) => ({ default: module.WorldMap })));

const PHONE_QUERY = '(max-width: 720px)';
/** Subscribes to the phone-width media query. */
function subscribePhone(onChange: () => void): () => void {
  const media = window.matchMedia(PHONE_QUERY);
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}

/** Shared owner-scoped map controller; the range line and any empty/location-sharing notice precede a nonshrinking plot and scrollable evidence. */
export function MapGadget({ instance, isEditMode = false }: { instance: GadgetInstance; isEditMode?: boolean }) {
  return instance.definition.filters?.map_mode === 'live' ? (
    <LiveMapGadget instance={instance} isEditMode={isEditMode} />
  ) : (
    <SelectedSourcesMapGadget instance={instance} isEditMode={isEditMode} />
  );
}

/** Observation features of the map (loaded page by page, up to 5 pages of 100). */
type MapObservations = ReturnType<typeof useMapObservations>['observations'];

/**
 * Reads the map scope of an instance: sources, regions, layers, precise-location opt-in, engine and a 1..30 day window.
 * Source and layer id lists are memoized so their empty-array fallbacks keep a stable identity for hook dependency lists.
 */
function useMapScope(instance: GadgetInstance) {
  const definition = instance.definition;
  const sourceIds = useMemo(() => definition.source_ids ?? [], [definition.source_ids]);
  const regions = definition.scope?.regions ?? [];
  const layerIds = useMemo(() => definition.scope?.map_layer_ids ?? [], [definition.scope?.map_layer_ids]);
  const precise = definition.filters?.show_precise_locations === true;
  const engine = definition.filters?.map_engine ?? 'globe';
  const days = Math.max(1, Math.min(definition.scope?.lookback_days ?? 30, 30));
  return { sourceIds, regions, layerIds, precise, engine, days };
}

/**
 * Loads the geospatial observations (only with precise locations enabled and at least one source) and projects them
 * to map features and layers.
 */
export function useMapObservations(instance: GadgetInstance, scope: ReturnType<typeof useMapScope>) {
  const { sourceIds, regions, layerIds, precise, days } = scope;
  const observations = useInfiniteQuery({
    queryKey: ['world-observations', 'map', instance.id, sourceIds, regions, days, precise],
    // The first page anchors the window at "now" on every fetch and refetch, so new observations appear. Later
    // pages reuse that anchor: the server binds each cursor to its from/to window and rejects a moved one (422).
    queryFn: async ({ pageParam, signal }) => {
      const to = pageParam.to ?? new Date();
      const page = await listGeospatialObservations(
        {
          sourceIds,
          regions,
          from: new Date(to.getTime() - days * 86_400_000),
          to,
          limit: 100,
          cursor: pageParam.cursor,
        },
        signal,
      );
      return { ...page, anchor: to };
    },
    initialPageParam: {} as { cursor?: string; to?: Date },
    getNextPageParam: (lastPage, pages) =>
      pages.length < 5 && lastPage.next_cursor ? { cursor: lastPage.next_cursor, to: lastPage.anchor } : undefined,
    enabled: precise && sourceIds.length > 0,
    staleTime: 60_000,
  });
  const features = useMemo(() => projectMapFeatures(observations.data?.pages.flatMap((page) => page.items) ?? []), [observations.data]);
  const layers = useMemo(() => buildMapLayers(sourceIds, layerIds, features, precise), [sourceIds, layerIds, features, precise]);
  const activeLayer = layers.find((layer) => layer.id === 'world_observations');
  const points = activeLayer?.enabled && activeLayer.availability === 'available' ? activeLayer.features : [];
  return { observations, features, activeLayer, points };
}

/**
 * Tracks the plot element's size (ResizeObserver) and whether it is on screen in a visible tab (IntersectionObserver +
 * visibilitychange). The plot node is state (callback ref) rather than a ref: the plot mounts and unmounts as the
 * empty/on-demand state changes, so observers must re-attach.
 */
function usePlotObservers() {
  const [plotNode, setPlotNode] = useState<HTMLDivElement | null>(null);
  const intersectingRef = useRef(false);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const node = plotNode;
    if (!node) return;
    const observer = new ResizeObserver(([entry]) =>
      setSize({
        width: Math.floor(entry.contentRect.width),
        height: Math.floor(entry.contentRect.height),
      }),
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [plotNode]);

  useEffect(() => {
    const node = plotNode;
    if (!node) return;
    const observer = new IntersectionObserver(([entry]) => {
      intersectingRef.current = entry.isIntersecting;
      setVisible(entry.isIntersecting && document.visibilityState === 'visible');
    });
    /** Tracks combined viewport intersection and document visibility to pause off-screen work. */
    const onVisibility = () => setVisible(intersectingRef.current && document.visibilityState === 'visible');
    observer.observe(node);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      observer.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [plotNode]);

  return { setPlotNode, size, visible };
}

/** Localized empty-state message: precise locations off or opt-in required, no source, layer disabled, or plain empty. */
function mapEmptyMessage(
  t: ReturnType<typeof useTranslations>,
  precise: boolean,
  layer: ReturnType<typeof useMapObservations>['activeLayer'],
): string {
  if (!precise || layer?.reason === 'privacy_opt_in_required') return t('mapPreciseDisabled');
  if (layer?.reason === 'no_source_selected') return t('mapSourceRequired');
  return layer?.enabled === false ? t('mapObservationLayerDisabled') : t('mapEmpty');
}

/** Detail card of the picked feature: metric, value, region/provider, source identity, quality and revision link. */
function SelectedFeatureCard({ selected }: { selected: NonNullable<ReturnType<typeof useMapObservations>['features'][number]> }) {
  const t = useTranslations('dashboard');
  return (
    <p className="absolute bottom-2 left-2 max-w-[90%] rounded bg-card/90 px-2 py-1 text-xs">
      {formatMetricLabel(selected.metric, t)} · {selected.value ?? t('weatherMissingValue')} {selected.unit} ·{' '}
      {selected.region ?? formatProviderName(selected.provider)}
      <span className="block">
        {t('mapSourceIdentity')}: {selected.source_id} · {t('mapQuality')}: {selected.quality}
      </span>
      {selected.document_version_number ? (
        <Link
          className="underline"
          href={`/knowledge/documents/${selected.document_id}?version=${selected.document_version_number}#cited-revision`}
        >
          {t('mapOpenRevision', { version: selected.document_version_number })}
        </Link>
      ) : (
        <span>{t('mapRevisionUnavailable')}</span>
      )}
    </p>
  );
}

/** Accessible evidence list: one button per feature, pagination and the truncation note. */
function MapEvidence({
  features,
  observations,
  selectedFeatureId,
  onPick,
}: {
  features: ReturnType<typeof useMapObservations>['features'];
  observations: MapObservations;
  selectedFeatureId: string | null;
  onPick: (featureId: string) => void;
}) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const truncated =
    observations.data?.pages.some((page) => page.truncated) ||
    ((observations.data?.pages.length ?? 0) >= 5 && Boolean(observations.data?.pages.at(-1)?.next_cursor));
  return (
    <div className={`flex min-h-0 flex-col ${features.length > 0 ? 'flex-1' : 'shrink-0'}`}>
      {features.length > 0 && (
        <h3 className="line-clamp-2 break-words text-xs font-semibold">{t('mapEvidenceList', { count: features.length })}</h3>
      )}
      <ul className="min-h-0 flex-1 divide-y divide-border overflow-y-auto" aria-label={t('mapEvidenceListLabel')}>
        {features.map((feature) => (
          <li key={feature.feature_id}>
            <BareButton
              type="button"
              onClick={() => onPick(feature.feature_id)}
              aria-pressed={feature.feature_id === selectedFeatureId}
              className="flex w-full items-start justify-between gap-2 py-1.5 text-left text-xs hover:bg-muted/30"
            >
              <span className="min-w-0 truncate">
                {formatMetricLabel(feature.metric, t)} · {feature.region ?? formatProviderName(feature.provider)}
                <time className="block text-xs text-muted-foreground" dateTime={feature.observed_at}>
                  {formatDateTime(feature.observed_at, display.locale, display.timezone)}
                </time>
              </span>
              <span className="shrink-0 tabular-nums">
                {feature.value ?? '—'} {feature.unit}
              </span>
            </BareButton>
          </li>
        ))}
      </ul>
      {observations.hasNextPage && (
        <BareButton
          type="button"
          disabled={observations.isFetchingNextPage}
          onClick={() => void observations.fetchNextPage()}
          className="line-clamp-2 max-w-full self-start break-words py-1 text-xs text-primary underline disabled:opacity-50"
        >
          {observations.isFetchingNextPage ? t('observationLoading') : t('mapLoadMore')}
        </BareButton>
      )}
      {truncated && (
        <p role="status" className="line-clamp-2 shrink-0 break-words text-xs text-muted-foreground">
          {t('observationTruncated')}
        </p>
      )}
    </div>
  );
}

/** Props the map engines share. */
type EngineProps = {
  features: ReturnType<typeof useMapObservations>['points'];
  selectedFeatureId: string | null;
  onSelectFeature: (featureId: string) => void;
  width: number;
  height: number;
  visible: boolean;
  interactive: boolean;
};

/** Notices above the plot: empty / location-sharing, missing source and load failure (the empty one moves into flow without a plot). */
function MapNotices({
  showPlot,
  mapEmpty,
  emptyMessage,
  needsSource,
  loadFailed,
}: {
  showPlot: boolean;
  mapEmpty: boolean;
  emptyMessage: string;
  needsSource: boolean;
  loadFailed: boolean;
}) {
  const t = useTranslations('dashboard');
  const empty = (
    <GadgetNotice
      kind="empty"
      message={emptyMessage}
      action={{ href: '/settings/sources', label: t('gadgetConfigureAction') }}
      className="shrink-0 px-0 py-1 text-xs"
    />
  );
  return (
    <>
      {/* Location-sharing / empty notice sits above the map in flow so it never covers the basemap. */}
      {showPlot && mapEmpty && empty}
      {needsSource && (
        <GadgetNotice
          kind="setup"
          message={t('mapSourceRequired')}
          action={{ href: '/settings/sources', label: t('gadgetConfigureAction') }}
          className="shrink-0 px-0 py-1 text-xs"
        />
      )}
      {loadFailed && <GadgetNotice kind="error" message={t('observationLoadFailed')} className="shrink-0 px-0 py-1 text-xs" />}
      {/* On-demand phone state with no points: the notice renders in normal flow (an absolute notice in a collapsed plot is clipped). */}
      {!showPlot && empty}
    </>
  );
}

/** The plot: phone opt-in prompt, loading hint, the lazily loaded engine, hide button and the picked-feature card. */
function MapPlot({
  setPlotNode,
  mapOnDemand,
  mapEmpty,
  mapRequested,
  setMapRequested,
  isPhone,
  loading,
  canRenderEngine,
  engine,
  engineProps,
  selected,
}: {
  setPlotNode: (node: HTMLDivElement | null) => void;
  mapOnDemand: boolean;
  mapEmpty: boolean;
  mapRequested: boolean;
  setMapRequested: (requested: boolean) => void;
  isPhone: boolean;
  loading: boolean;
  canRenderEngine: boolean;
  engine: string;
  engineProps: EngineProps;
  selected: ReturnType<typeof useMapObservations>['features'][number] | null;
}) {
  const t = useTranslations('dashboard');
  const rendererLoading = (
    <p role="status" className="p-2 text-xs text-muted-foreground">
      {t('mapRendererLoading')}
    </p>
  );
  return (
    <div
      ref={setPlotNode}
      role="group"
      className="relative min-h-48 flex-[2] overflow-hidden rounded border border-border"
      aria-label={t('mapPlotLabel')}
    >
      {mapOnDemand && !mapEmpty && (
        <div className="flex h-full flex-col items-start justify-center gap-2 p-3">
          <p className="text-xs text-muted-foreground">{t('mapOnDemand')}</p>
          <Button type="button" variant="outline" onClick={() => setMapRequested(true)}>
            {t('mapOpen')}
          </Button>
        </div>
      )}
      {/* Loading sits over the plot instead of a separate skeleton block, so a drawn map is never pushed down. */}
      {loading && (
        <p role="status" className="absolute left-2 top-2 z-10 m-0 rounded bg-card/90 px-2 py-1 text-xs text-muted-foreground">
          {t('observationLoading')}
        </p>
      )}
      {/* Engines paint into an absolute layer so the canvas never sizes the plot: the plot takes its height from the card's flex layout, and the footer stays inside the card. */}
      {canRenderEngine && (
        <div className="absolute inset-0">
          <Suspense fallback={rendererLoading}>
            <WorldMap {...engineProps} projection={engine === 'globe' ? 'globe' : 'mercator'} />
          </Suspense>
        </div>
      )}
      {isPhone && mapRequested && (
        <Button type="button" variant="outline" className="absolute right-2 top-2 z-10" onClick={() => setMapRequested(false)}>
          {t('mapHide')}
        </Button>
      )}
      {selected && <SelectedFeatureCard selected={selected} />}
    </div>
  );
}

/**
 * Derived plot flags. On phones the WebGL map is opt-in (`mapOnDemand`); the engine renders only when on screen with a
 * measured size. Without points the base map stays visible and the empty notice is overlaid on it; with precise
 * locations off the observations query never runs, so that state counts as empty at once. The phone on-demand empty
 * state skips the plot entirely (its notice renders in flow and no credit is shown).
 */
function mapViewState(i: {
  isPhone: boolean;
  mapRequested: boolean;
  visible: boolean;
  size: { width: number; height: number };
  pointCount: number;
  precise: boolean;
  observationsPending: boolean;
}) {
  const mapOnDemand = i.isPhone && !i.mapRequested;
  const canRenderEngine = i.visible && i.size.width > 0 && i.size.height > 0 && !mapOnDemand;
  const mapEmpty = !i.pointCount && (!i.precise || !i.observationsPending);
  return { mapOnDemand, canRenderEngine, mapEmpty, showPlot: !(mapOnDemand && mapEmpty) };
}

/** Render the legacy authorized observation map without changing its saved source semantics. */
function SelectedSourcesMapGadget({ instance, isEditMode = false }: { instance: GadgetInstance; isEditMode?: boolean }) {
  const t = useTranslations('dashboard');
  const scope = useMapScope(instance);
  const { sourceIds, precise, engine, days } = scope;
  const { setPlotNode, size, visible } = usePlotObservers();
  const [pickedFeatureId, setSelectedFeatureId] = useState<string | null>(null);
  const isPhone = useSyncExternalStore(
    subscribePhone,
    () => window.matchMedia(PHONE_QUERY).matches,
    () => false,
  );
  const [mapRequested, setMapRequested] = useState(false);
  const { observations, features, activeLayer, points } = useMapObservations(instance, scope);

  const selectedFeatureId = pickedFeatureId && features.some((feature) => feature.feature_id === pickedFeatureId) ? pickedFeatureId : null;
  /** Stable handler passed to the map renderers: records the picked feature id (validated against loaded features before use). */
  const selectFeature = useCallback((featureId: string) => setSelectedFeatureId(featureId), []);
  const selected = features.find((feature) => feature.feature_id === selectedFeatureId) ?? null;
  const { mapOnDemand, canRenderEngine, mapEmpty, showPlot } = mapViewState({
    isPhone,
    mapRequested,
    visible,
    size,
    pointCount: points.length,
    precise,
    observationsPending: observations.isPending,
  });
  const emptyMessage = mapEmptyMessage(t, precise, activeLayer);
  const engineProps = {
    features: points,
    selectedFeatureId,
    onSelectFeature: selectFeature,
    width: size.width,
    height: size.height,
    visible,
    interactive: !isEditMode,
  };
  return (
    <section className="flex h-full min-h-0 flex-col gap-2 overflow-hidden bg-card p-3 text-card-foreground">
      {/* The gadget frame already shows the title; keep only the range line (the engine label is configuration noise). */}
      <p className="m-0 line-clamp-2 shrink-0 break-words text-xs text-muted-foreground">{t('mapRange', { days })}</p>
      <MapNotices
        showPlot={showPlot}
        mapEmpty={mapEmpty}
        emptyMessage={emptyMessage}
        needsSource={precise && sourceIds.length === 0}
        loadFailed={observations.isError}
      />
      {showPlot && (
        <MapPlot
          setPlotNode={setPlotNode}
          mapOnDemand={mapOnDemand}
          mapEmpty={mapEmpty}
          mapRequested={mapRequested}
          setMapRequested={setMapRequested}
          isPhone={isPhone}
          loading={precise && sourceIds.length > 0 && observations.isPending}
          canRenderEngine={canRenderEngine}
          engine={engine}
          engineProps={engineProps}
          selected={selected}
        />
      )}
      <MapEvidence features={features} observations={observations} selectedFeatureId={selectedFeatureId} onPick={setSelectedFeatureId} />
      {/* The basemap credit describes a drawn map, so it shows only when the plot is rendered. This gadget has no weather overlay, so no Open-Meteo credit. */}
      {showPlot && <p className="line-clamp-2 shrink-0 break-words text-xs text-muted-foreground">{t('mapBasemapCredit')}</p>}
    </section>
  );
}
