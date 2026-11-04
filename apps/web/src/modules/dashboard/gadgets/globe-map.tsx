'use client';

import GlobeRenderer, { type GlobeInstance } from 'globe.gl';
import { CanvasTexture, MeshBasicMaterial, SRGBColorSpace } from 'three';
import { feature } from 'topojson-client';
import worldTopology from 'world-atlas/countries-110m.json';
import type { GeometryCollection, Topology } from 'topojson-specification';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { FlatMap } from './flat-map';
import { useWeatherRaster, type WeatherProps } from './weather-raster';

/** Coordinates and stable identity shared by evidence and live-event map points. */
export type MapPoint = {
  feature_id: string;
  latitude: number;
  longitude: number;
  metric?: string;
  observed_at?: string;
};

/** Props shared by the accessible map controller and the lazy 3D renderer. */
export type GlobeMapProps = WeatherProps & {
  features: MapPoint[];
  selectedFeatureId: string | null;
  onSelectFeature: (featureId: string) => void;
  width: number;
  height: number;
  visible: boolean;
  interactive: boolean;
  focusSelected?: boolean;
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

/** Identify a renderable point from globe.gl's object-accessor input. */
function isMapFeature(value: object): value is MapPoint {
  return 'feature_id' in value && typeof value.feature_id === 'string'
    && 'latitude' in value && typeof value.latitude === 'number'
    && 'longitude' in value && typeof value.longitude === 'number';
}

/**
 * Centroid of the evidence points as a globe point of view; averaging unit vectors keeps the result correct across the antimeridian.
 *
 * @param points Map features with WGS84 coordinates.
 * @returns lat/lng in degrees, or null when there are no points (callers keep the default view).
 */
export function centroidView(points: Array<{ latitude: number; longitude: number }>): { lat: number; lng: number } | null {
  if (!points.length) return null;
  const rad = Math.PI / 180;
  let x = 0, y = 0, z = 0;
  for (const p of points) {
    x += Math.cos(p.latitude * rad) * Math.cos(p.longitude * rad);
    y += Math.cos(p.latitude * rad) * Math.sin(p.longitude * rad);
    z += Math.sin(p.latitude * rad);
  }
  if (Math.hypot(x, y, z) < 1e-6) return null;
  return { lat: Math.atan2(z, Math.hypot(x, y)) / rad, lng: Math.atan2(y, x) / rad };
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
 * Render a sphere with optional same-origin weather imagery in a nonshrinking 192px plot.
 * When the globe cannot be created or its context is lost, the FlatMap is rendered over it with a visible note.
 */
export function GlobeMap({ features, selectedFeatureId, onSelectFeature, width, height, visible, interactive, weatherLayer, weatherWorkspaceId, weatherRevision, onWeatherStateChange, focusSelected = false }: GlobeMapProps) {
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
  const failed = unavailable || contextLost;
  const weather = useWeatherRaster({ weatherLayer, weatherWorkspaceId, weatherRevision, onWeatherStateChange: failed ? undefined : onWeatherStateChange }, visible && !failed && isSized);
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
        // Radius is in degrees of arc: 0.48 was under one pixel on a ~190px sphere. 1.6 reads as a ~3px dot, and the
        // altitude lifts it clear of the land cap (0.004) so the dot is never z-fought into the polygon fill.
        // globe.gl reads `lat`/`lng` by default; MapFeature uses latitude/longitude, so without these accessors every
        // point resolved to NaN and nothing was drawn.
        .pointLat((feature) => isMapFeature(feature) ? feature.latitude : 0)
        .pointLng((feature) => isMapFeature(feature) ? feature.longitude : 0)
        .pointRadius(1.6)
        .pointAltitude(0.015)
        .pointColor((feature) => isMapFeature(feature) && feature.feature_id === selectedFeatureRef.current
          ? readMapToken('--accent', '#176a4a')
          : readMapToken('--chart-1', '#176a4a'))
        .pointLabel((feature) => isMapFeature(feature) ? [feature.metric, feature.observed_at].filter(Boolean).join(' ? ') : '')
        .onPointClick((feature) => { if (isMapFeature(feature)) onSelectFeature(feature.feature_id); });
      renderer = activeRenderer;
      rendererRef.current = activeRenderer;
      materialRef.current = activeMaterial;
      activeRenderer.pointsData(features).width(width).height(height);
      activeRenderer.pointOfView({ ...centroidView(features), altitude: fitAltitude(width, height) });
      activeRenderer.controls().enabled = visible && interactive;
      // A context created while the browser's context budget is exhausted is lost before any listener can attach; detect it now.
      if (activeRenderer.renderer().getContext().isContextLost()) throw new Error('WebGL context lost on creation');
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
      // The renderer can be built while the lazy chunk/layout is still settling and then never paint (stale size, paused loop,
      // or a context that died silently). Re-apply size and resume on the next frame, then verify the context one frame later
      // so a dead context falls back to the flat map instead of leaving an empty plot.
      let frameB = 0;
      const frameA = requestAnimationFrame(() => {
        activeRenderer.width(width).height(height);
        if (visibleRef.current) activeRenderer.resumeAnimation();
        frameB = requestAnimationFrame(() => {
          if (activeRenderer.renderer().getContext().isContextLost()) setContextLost(true);
        });
      });
      return () => {
        cancelAnimationFrame(frameA);
        cancelAnimationFrame(frameB);
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

  const focused = focusSelected ? features.find((item) => item.feature_id === selectedFeatureId) : undefined;
  const focusedLat = focused?.latitude, focusedLon = focused?.longitude;
  const hasFocus = focusedLat !== undefined && focusedLon !== undefined;
  useEffect(() => {
    if (focusedLat !== undefined && focusedLon !== undefined) rendererRef.current?.pointOfView({ lat: focusedLat, lng: focusedLon }, 500);
  }, [selectedFeatureId, focusedLat, focusedLon]);

  const center = centroidView(features);
  const centerLat = center?.lat;
  const centerLng = center?.lng;
  /** Re-center only when the geographic centroid changes, preserving user rotation across data refreshes. */
  useEffect(() => {
    if (!hasFocus && centerLat !== undefined && centerLng !== undefined) {
      rendererRef.current?.pointOfView({ lat: centerLat, lng: centerLng });
    }
  }, [centerLat, centerLng, hasFocus]);

  useEffect(() => {
    const renderer = rendererRef.current;
    const material = materialRef.current;
    if (!renderer || !material) return;
    const observer = new MutationObserver(() => {
      if (material.map && weather) {
        const canvas = material.map.image as HTMLCanvasElement;
        const context = canvas.getContext('2d');
        if (context) {
          context.fillStyle = readGlobeColors().ocean;
          context.fillRect(0, 0, canvas.width, canvas.height);
          context.drawImage(weather, 0, 0);
          material.map.needsUpdate = true;
        }
      } else material.color.setStyle(readGlobeColors().ocean);
      renderer.polygonCapColor(() => material.map ? 'rgba(0,0,0,0)' : readGlobeColors().land).polygonSideColor(() => readGlobeColors().land).polygonStrokeColor(() => readGlobeColors().stroke);
      renderer.pointColor((feature) => isMapFeature(feature) && feature.feature_id === selectedFeatureRef.current
        ? readMapToken('--accent', '#176a4a') : readMapToken('--chart-1', '#176a4a'));
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => observer.disconnect();
  }, [isSized, onSelectFeature, selectedFeatureId, weather]);

  useEffect(() => {
    const renderer = rendererRef.current;
    const material = materialRef.current;
    if (!renderer || !material) return;
    let texture: CanvasTexture | null = null;
    if (weather) {
      const canvas = document.createElement('canvas');
      canvas.width = weather.width;
      canvas.height = weather.height;
      const context = canvas.getContext('2d');
      if (context) {
        context.fillStyle = readGlobeColors().ocean;
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(weather, 0, 0);
        texture = new CanvasTexture(canvas);
        texture.colorSpace = SRGBColorSpace;
      }
    }
    material.map = texture;
    if (texture) material.color.setRGB(1, 1, 1);
    else material.color.setStyle(readGlobeColors().ocean);
    material.needsUpdate = true;
    // Transparent land caps keep outlines above the weather texture without hiding it.
    renderer.polygonCapColor(() => texture ? 'rgba(0,0,0,0)' : readGlobeColors().land);
    return () => {
      material.map = null;
      material.needsUpdate = true;
      texture?.dispose();
    };
  }, [weather, isSized, onSelectFeature]);

  return <div className="relative h-full min-h-48 shrink-0 w-full" aria-hidden={failed ? undefined : 'true'}>
    <div ref={hostRef} className={failed ? 'hidden' : 'h-full w-full'} />
    {failed && <>
      <FlatMap features={features} selectedFeatureId={selectedFeatureId} onSelectFeature={onSelectFeature} width={width} height={height} visible={visible} interactive={interactive} weatherLayer={weatherLayer} weatherWorkspaceId={weatherWorkspaceId} weatherRevision={weatherRevision} onWeatherStateChange={onWeatherStateChange} focusSelected={focusSelected} />
      <p role="status" className="absolute bottom-2 left-2 rounded bg-card px-2 py-1 text-xs text-muted-foreground">{t('mapWebglUnavailable')}</p>
    </>}
  </div>;
}
