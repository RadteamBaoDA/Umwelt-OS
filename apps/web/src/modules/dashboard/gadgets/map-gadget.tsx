'use client';

import { useInfiniteQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import Link from 'next/link';
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listGeospatialObservations } from '@/modules/observations/api';
import type { GadgetInstance } from '../api';
import { buildMapLayers, projectMapFeatures } from '../map-layers';

const FlatMap = lazy(() => import('./flat-map').then((module) => ({ default: module.FlatMap })));
const GlobeMap = lazy(() => import('./globe-map').then((module) => ({ default: module.GlobeMap })));

/** Shared owner-scoped map controller; both engines consume one feature set and selection state. */
export function MapGadget({ instance, isEditMode = false }: { instance: GadgetInstance; isEditMode?: boolean }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const definition = instance.definition;
  const sourceIds = definition.source_ids ?? [];
  const regions = definition.scope?.regions ?? [];
  const layerIds = definition.scope?.map_layer_ids ?? [];
  const precise = definition.filters?.show_precise_locations === true;
  const engine = definition.filters?.map_engine ?? 'globe';
  const days = Math.max(1, Math.min(definition.scope?.lookback_days ?? 30, 30));
  const to = useMemo(() => new Date(), [instance.id]);
  const from = useMemo(() => new Date(to.getTime() - days * 86_400_000), [to, days]);
  const plotRef = useRef<HTMLDivElement>(null);
  const intersectingRef = useRef(false);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [visible, setVisible] = useState(false);
  const [selectedFeatureId, setSelectedFeatureId] = useState<string | null>(null);

  useEffect(() => {
    const node = plotRef.current;
    if (!node) return;
    const observer = new ResizeObserver(([entry]) => setSize({
      width: Math.floor(entry.contentRect.width), height: Math.floor(entry.contentRect.height),
    }));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const node = plotRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(([entry]) => {
      intersectingRef.current = entry.isIntersecting;
      setVisible(entry.isIntersecting && document.visibilityState === 'visible');
    });
    const onVisibility = () => setVisible(intersectingRef.current && document.visibilityState === 'visible');
    observer.observe(node);
    document.addEventListener('visibilitychange', onVisibility);
    return () => { observer.disconnect(); document.removeEventListener('visibilitychange', onVisibility); };
  }, []);

  const observations = useInfiniteQuery({
    queryKey: ['world-observations', 'map', instance.id, sourceIds, regions, days, precise],
    queryFn: ({ pageParam, signal }) => listGeospatialObservations({
      sourceIds, regions, from, to, limit: 100, cursor: pageParam,
    }, signal),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage, pages) => pages.length < 5 ? lastPage.next_cursor ?? undefined : undefined,
    enabled: precise && sourceIds.length > 0,
    staleTime: 60_000,
  });

  const features = useMemo(() => projectMapFeatures(observations.data?.pages.flatMap((page) => page.items) ?? []), [observations.data]);
  const layers = useMemo(() => buildMapLayers(sourceIds, layerIds, features, precise), [sourceIds, layerIds, features, precise]);
  const activeLayer = layers.find((layer) => layer.id === 'world_observations');
  const points = activeLayer?.enabled && activeLayer.availability === 'available' ? activeLayer.features : [];
  const selectFeature = useCallback((featureId: string) => setSelectedFeatureId(featureId), []);
  const selected = features.find((feature) => feature.feature_id === selectedFeatureId) ?? null;
  const canRenderEngine = visible && size.width > 0 && size.height > 0;

  useEffect(() => {
    if (selectedFeatureId && !features.some((feature) => feature.feature_id === selectedFeatureId)) {
      setSelectedFeatureId(null);
    }
  }, [features, selectedFeatureId]);

  return <section className="flex h-full min-h-0 flex-col gap-2 overflow-hidden bg-card p-3 text-card-foreground">
    <header className="flex items-center justify-between gap-2 border-b border-border pb-2">
      <div><h2 className="text-sm font-semibold">{instance.title || t('mapTitle')}</h2><p className="text-xs text-muted-foreground">{t('mapRange', { days })}</p></div>
      <span className="text-xs text-muted-foreground">{engine === 'globe' ? t('mapGlobeEngine') : t('mapFlatEngine')}</span>
    </header>
    {!precise && <p role="status" className="text-xs text-muted-foreground">{t('mapPreciseDisabled')}</p>}
    {precise && sourceIds.length === 0 && <p role="status" className="text-xs text-muted-foreground">{t('mapSourceRequired')}</p>}
    {precise && sourceIds.length > 0 && observations.isPending && <p role="status" className="text-xs text-muted-foreground">{t('observationLoading')}</p>}
    {observations.isError && <p role="alert" className="text-xs text-destructive">{t('observationLoadFailed')}</p>}
    <div ref={plotRef} role="group" className="relative min-h-48 flex-1 overflow-hidden rounded border border-border" aria-label={t('mapPlotLabel')}>
      {canRenderEngine && engine === 'globe'
        ? <Suspense fallback={<p role="status" className="p-2 text-xs text-muted-foreground">{t('mapRendererLoading')}</p>}><GlobeMap features={points} selectedFeatureId={selectedFeatureId} onSelectFeature={selectFeature} width={size.width} height={size.height} visible={visible} interactive={!isEditMode} /></Suspense>
        : canRenderEngine && <Suspense fallback={<p role="status" className="p-2 text-xs text-muted-foreground">{t('mapRendererLoading')}</p>}><FlatMap features={points} selectedFeatureId={selectedFeatureId} onSelectFeature={selectFeature} width={size.width} height={size.height} visible={visible} interactive={!isEditMode} /></Suspense>}
      {selected && <p className="absolute bottom-2 left-2 max-w-[90%] rounded bg-card/90 px-2 py-1 text-xs">{selected.metric} · {selected.value ?? t('weatherMissingValue')} {selected.unit} · {selected.region ?? selected.provider}<span className="block">{t('mapSourceIdentity')}: {selected.source_id} · {t('mapQuality')}: {selected.quality}</span>{selected.document_version_number ? <Link className="underline" href={`/knowledge/documents/${selected.document_id}?version=${selected.document_version_number}#cited-revision`}>{t('mapOpenRevision', { version: selected.document_version_number })}</Link> : <span>{t('mapRevisionUnavailable')}</span>}</p>}
    </div>
    <div className="flex min-h-0 flex-1 flex-col">
      <h3 className="text-xs font-semibold">{t('mapEvidenceList', { count: features.length })}</h3>
      {!points.length && precise && !observations.isPending && <p role="status" className="py-2 text-xs text-muted-foreground">{activeLayer?.reason === 'privacy_opt_in_required' ? t('mapPreciseDisabled') : activeLayer?.reason === 'no_source_selected' ? t('mapSourceRequired') : activeLayer?.enabled === false ? t('mapObservationLayerDisabled') : t('observationEmpty')}</p>}
      <ul className="min-h-0 flex-1 divide-y divide-border overflow-y-auto" aria-label={t('mapEvidenceListLabel')}>
        {features.map((feature) => <li key={feature.feature_id}>
          <button type="button" onClick={() => setSelectedFeatureId(feature.feature_id)} aria-pressed={feature.feature_id === selectedFeatureId} className="flex w-full items-start justify-between gap-2 py-1.5 text-left text-xs hover:bg-muted/30">
            <span className="min-w-0 truncate">{feature.metric} · {feature.region ?? feature.provider}<time className="block text-[10px] text-muted-foreground" dateTime={feature.observed_at}>{formatDateTime(feature.observed_at, display.locale, display.timezone)}</time></span>
            <span className="shrink-0 font-mono">{feature.value ?? '—'} {feature.unit}</span>
          </button>
        </li>)}
      </ul>
      {observations.hasNextPage && <button type="button" disabled={observations.isFetchingNextPage} onClick={() => void observations.fetchNextPage()} className="self-start py-1 text-xs text-primary underline disabled:opacity-50">{observations.isFetchingNextPage ? t('observationLoading') : t('mapLoadMore')}</button>}
      {(observations.data?.pages.some((page) => page.truncated) || ((observations.data?.pages.length ?? 0) >= 5 && Boolean(observations.data?.pages.at(-1)?.next_cursor))) && <p role="status" className="text-xs text-muted-foreground">{t('observationTruncated')}</p>}
    </div>
    <p className="text-[10px] text-muted-foreground"><a className="underline" href="https://open-meteo.com/" target="_blank" rel="noreferrer">{t('openMeteoAttribution')}</a> · {t('mapNoRemoteTiles')}</p>
  </section>;
}
