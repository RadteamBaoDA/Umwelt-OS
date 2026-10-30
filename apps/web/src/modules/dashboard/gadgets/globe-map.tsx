'use client';

import GlobeRenderer, { type GlobeInstance } from 'globe.gl';
import { MeshBasicMaterial } from 'three';
import { feature } from 'topojson-client';
import worldTopology from 'world-atlas/countries-110m.json';
import type { GeometryCollection, Topology } from 'topojson-specification';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import type { MapFeature } from '../map-layers';
import { FlatMap } from './flat-map';

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

/** True for the only colour syntaxes globe.gl's polished parser accepts here: hex, rgb(a) and hsl(a). */
const PARSEABLE_COLOR = /^(#[0-9a-f]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\))$/i;

/**
 * Read a semantic CSS color at render time so the Three material follows light and dark themes.
 * Returns the hex fallback whenever the computed value is empty or not hex/rgb/hsl, because polished throws on other strings.
 */
function readMapToken(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return PARSEABLE_COLOR.test(value) ? value : fallback;
}

/** Apply an alpha to a #rgb/#rrggbb token as an rgba() string; other syntaxes are returned unchanged. */
function withAlpha(color: string, alpha: number): string {
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color)?.[1];
  if (!hex) return color;
  const full = hex.length === 3 ? [...hex].map((c) => c + c).join('') : hex;
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
  return `rgba(${r},${g},${b},${alpha})`;
}

/** Theme colours for the sphere (ocean), land and land outlines; both themes stay legible against the card. */
function readGlobeColors() {
  return {
    ocean: readMapToken('--active', '#e8ece8'),
    land: readMapToken('--control', '#cfd5d0'),
    stroke: withAlpha(readMapToken('--muted', '#5c6660'), 0.35),
  };
}

/**
 * Camera altitude (in globe radii) at which the sphere's diameter is ~90% of the plot's shorter side.
 * globe.gl uses a 50deg vertical FOV and camera distance R*(1+altitude); solving sin(theta)=1/(1+alt) for the target angular radius.
 */
function fitAltitude(width: number, height: number): number {
  const x = 0.9 * Math.min(width, height) / height * Math.tan(Math.PI / 7.2);
  return Math.sqrt(1 + x * x) / x - 1;
}

/** Narrow the globe.gl object-accessor contract to this map's detached evidence DTO. */
function isMapFeature(value: object): value is MapFeature {
  return 'feature_id' in value && typeof value.feature_id === 'string'
    && 'metric' in value && typeof value.metric === 'string'
    && 'observed_at' in value && typeof value.observed_at === 'string';
}

/** Convert the bundled Natural Earth countries once; the globe never fetches remote imagery. */
const countryTopology = worldTopology as unknown as Topology<{ countries: GeometryCollection }>;
const countries = feature(countryTopology, countryTopology.objects.countries).features;

/** Throw when the browser cannot create a WebGL context, so callers can fall back before Three.js fails silently. */
function assertWebGLAvailable(): void {
  const probe = document.createElement('canvas');
  if (!probe.getContext('webgl2') && !probe.getContext('webgl')) throw new Error('WebGL is unavailable');
}

/**
 * Render a local bare sphere in a nonshrinking 192px plot without requesting remote imagery or tiles.
 * When the globe cannot be created or its context is lost, the FlatMap is rendered over it with a visible note.
 */
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
  useLayoutEffect(() => {
    selectedFeatureRef.current = selectedFeatureId;
    visibleRef.current = visible;
    interactiveRef.current = interactive;
  });

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !isSized) return;
    let renderer: GlobeInstance | null = null;
    let material: MeshBasicMaterial | null = null;
    try {
      assertWebGLAvailable();
      const activeMaterial = new MeshBasicMaterial({ color: readGlobeColors().ocean });
      material = activeMaterial;
      const activeRenderer = new GlobeRenderer(host, { rendererConfig: { antialias: true, alpha: true } })
        // globe.gl parses colours with polished, which has no `transparent` keyword (throws error #5 and the globe never draws).
        .backgroundColor('rgba(0,0,0,0)')
        .globeMaterial(activeMaterial)
        .showAtmosphere(false)
        .showGraticules(true)
        .polygonsData(countries)
        .polygonAltitude(0.004)
        .polygonCapColor(() => readGlobeColors().land)
        .polygonSideColor(() => readGlobeColors().land)
        .polygonStrokeColor(() => readGlobeColors().stroke)
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
      activeRenderer.pointOfView({ altitude: fitAltitude(width, height) });
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
      // Renderer creation is an external-system sync; clearing the failure flag belongs here.
      // eslint-disable-next-line react-hooks/set-state-in-effect
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
    } catch (error) {
      console.warn('Globe renderer unavailable, using flat map:', error instanceof Error ? error.message : error);
      if (renderer) {
        renderer._destructor();
        renderer.renderer().forceContextLoss();
      }
      material?.dispose();
      setUnavailable(true);
    }
  }, [isSized, onSelectFeature]);

  /** Re-fit the sphere to ~90% of the plot after a resize, keeping the user's current rotation. */
  useEffect(() => {
    const renderer = rendererRef.current;
    if (!renderer || !isSized) return;
    const { lat, lng } = renderer.pointOfView();
    renderer.width(width).height(height).pointOfView({ lat, lng, altitude: fitAltitude(width, height) });
  }, [height, isSized, width]);

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
      material.color.setStyle(readGlobeColors().ocean);
      renderer.polygonCapColor(() => readGlobeColors().land).polygonSideColor(() => readGlobeColors().land).polygonStrokeColor(() => readGlobeColors().stroke);
      renderer.pointColor((feature) => isMapFeature(feature) && feature.feature_id === selectedFeatureRef.current
        ? readMapToken('--accent', '#176a4a') : readMapToken('--chart-1', '#176a4a'));
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => observer.disconnect();
  }, [isSized, selectedFeatureId]);

  const failed = unavailable || contextLost;
  return <div className="relative h-full min-h-48 shrink-0 w-full" aria-hidden={failed ? undefined : 'true'}>
    <div ref={hostRef} className={failed ? 'hidden' : 'h-full w-full'} />
    {failed && <>
      <FlatMap features={features} selectedFeatureId={selectedFeatureId} onSelectFeature={onSelectFeature} width={width} height={height} visible={visible} interactive={interactive} />
      <p role="status" className="absolute bottom-2 left-2 rounded bg-card px-2 py-1 text-xs text-muted-foreground">{t('mapWebglUnavailable')}</p>
    </>}
  </div>;
}
