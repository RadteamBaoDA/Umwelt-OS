'use client';

import GlobeRenderer, { type GlobeInstance } from 'globe.gl';
import { MeshBasicMaterial } from 'three';
import { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import type { MapFeature } from '../map-layers';

/** Props shared by the accessible map controller and the lazy 3D renderer. */
export type GlobeMapProps = {
  features: MapFeature[];
  selectedFeatureId: string | null;
  onSelectFeature: (featureId: string) => void;
  width: number;
  height: number;
  visible: boolean;
  interactive: boolean;
};

/** Read a semantic CSS color at render time so the Three material follows light and dark themes. */
function readMapToken(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

/** Narrow the globe.gl object-accessor contract to this map's detached evidence DTO. */
function isMapFeature(value: object): value is MapFeature {
  return 'feature_id' in value && typeof value.feature_id === 'string'
    && 'metric' in value && typeof value.metric === 'string'
    && 'observed_at' in value && typeof value.observed_at === 'string';
}

/** Render a local bare sphere and shared point IDs without requesting remote globe imagery or tiles. */
export function GlobeMap({ features, selectedFeatureId, onSelectFeature, width, height, visible, interactive }: GlobeMapProps) {
  const t = useTranslations('dashboard');
  const hostRef = useRef<HTMLDivElement>(null);
  const rendererRef = useRef<GlobeInstance | null>(null);
  const materialRef = useRef<MeshBasicMaterial | null>(null);
  const selectedFeatureRef = useRef<string | null>(selectedFeatureId);
  const visibleRef = useRef(visible);
  const interactiveRef = useRef(interactive);
  const [unavailable, setUnavailable] = useState(false);
  const [contextLost, setContextLost] = useState(false);
  const isSized = width > 0 && height > 0;
  selectedFeatureRef.current = selectedFeatureId;
  visibleRef.current = visible;
  interactiveRef.current = interactive;

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !isSized) return;
    let renderer: GlobeInstance | null = null;
    let material: MeshBasicMaterial | null = null;
    try {
      const activeMaterial = new MeshBasicMaterial({ color: readMapToken('--surface', '#ffffff') });
      material = activeMaterial;
      const activeRenderer = new GlobeRenderer(host, { rendererConfig: { antialias: true, alpha: true } })
        .backgroundColor('transparent')
        .globeMaterial(activeMaterial)
        .showAtmosphere(false)
        .showGraticules(true)
        .pointRadius(0.48)
        .pointAltitude(0.008)
        .pointColor((feature) => isMapFeature(feature) && feature.feature_id === selectedFeatureRef.current
          ? readMapToken('--accent', '#176a4a')
          : readMapToken('--chart-1', '#176a4a'))
        .pointLabel((feature) => isMapFeature(feature) ? `${feature.metric} · ${feature.observed_at}` : '')
        .onPointClick((feature) => { if (isMapFeature(feature)) onSelectFeature(feature.feature_id); });
      renderer = activeRenderer;
      rendererRef.current = activeRenderer;
      materialRef.current = activeMaterial;
      activeRenderer.pointsData(features).width(width).height(height);
      activeRenderer.controls().enabled = visible && interactive;
      const canvas = activeRenderer.renderer().domElement;
      const handleContextLost = (event: Event) => {
        // Keep browser restoration enabled and expose the same evidence list as the fallback.
        event.preventDefault();
        setContextLost(true);
        activeRenderer.pauseAnimation();
      };
      const handleContextRestored = () => {
        setContextLost(false);
        if (visibleRef.current) {
          activeRenderer.controls().enabled = interactiveRef.current;
          activeRenderer.resumeAnimation();
        }
      };
      canvas.addEventListener('webglcontextlost', handleContextLost);
      canvas.addEventListener('webglcontextrestored', handleContextRestored);
      setUnavailable(false);
      return () => {
        canvas.removeEventListener('webglcontextlost', handleContextLost);
        canvas.removeEventListener('webglcontextrestored', handleContextRestored);
        rendererRef.current = null;
        materialRef.current = null;
        activeRenderer._destructor();
        activeRenderer.renderer().forceContextLoss();
        activeMaterial.dispose();
      };
    } catch {
      if (renderer) {
        renderer._destructor();
        renderer.renderer().forceContextLoss();
      }
      material?.dispose();
      setUnavailable(true);
    }
  }, [isSized, onSelectFeature]);

  useEffect(() => {
    const renderer = rendererRef.current;
    if (!renderer) return;
    renderer.pointsData(features).width(width).height(height);
    renderer.controls().enabled = visible && interactive;
    if (visible) renderer.resumeAnimation();
    else renderer.pauseAnimation();
  }, [features, height, interactive, selectedFeatureId, visible, width]);

  useEffect(() => {
    const renderer = rendererRef.current;
    const material = materialRef.current;
    if (!renderer || !material) return;
    const observer = new MutationObserver(() => {
      material.color.setStyle(readMapToken('--surface', '#ffffff'));
      renderer.pointColor((feature) => isMapFeature(feature) && feature.feature_id === selectedFeatureRef.current
        ? readMapToken('--accent', '#176a4a') : readMapToken('--chart-1', '#176a4a'));
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => observer.disconnect();
  }, [isSized, selectedFeatureId]);

  return <div className="relative h-full min-h-48 w-full" aria-hidden="true">
    <div ref={hostRef} className="h-full w-full" />
    {(unavailable || contextLost) && <span role="status" className="sr-only">{t('mapWebglUnavailable')}</span>}
  </div>;
}
