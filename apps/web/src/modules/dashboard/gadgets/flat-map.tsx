'use client';

import { DeckGL } from '@deck.gl/react';
import { BitmapLayer, GeoJsonLayer, ScatterplotLayer } from '@deck.gl/layers';
import { OrthographicView } from '@deck.gl/core';
import { feature } from 'topojson-client';
import worldTopology from 'world-atlas/countries-110m.json';
import type { Topology } from 'topojson-specification';
import type { FeatureCollection } from 'geojson';
import { useTranslations } from 'next-intl';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { MapPoint } from './globe-map';
import { useWeatherRaster, type WeatherProps } from './weather-raster';

/** Props shared by the accessible map controller and local deck.gl renderer. */
export type FlatMapProps = WeatherProps & {
  features: MapPoint[];
  selectedFeatureId: string | null;
  onSelectFeature: (featureId: string) => void;
  width: number;
  height: number;
  visible: boolean;
  interactive: boolean;
  focusSelected?: boolean;
};

/** Convert the active semantic CSS token to deck.gl's numeric color contract. */
function readDeckColor(token: string, fallback: string, alpha: number): [number, number, number, number] {
  const value = typeof window === 'undefined'
    ? fallback : getComputedStyle(document.documentElement).getPropertyValue(token).trim() || fallback;
  const shortHex = /^#([\da-f])([\da-f])([\da-f])$/i.exec(value);
  const longHex = /^#([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(value);
  const rgb = shortHex
    ? shortHex.slice(1).map((part) => Number.parseInt(`${part}${part}`, 16))
    : longHex ? longHex.slice(1).map((part) => Number.parseInt(part, 16)) : null;
  return rgb ? [rgb[0], rgb[1], rgb[2], alpha] : [23, 106, 74, alpha];
}

/**
 * Unwrap a ring continuously across the antimeridian: walk the points keeping a running +/-360 offset that
 * absorbs every raw longitude jump larger than 180 degrees, so western longitudes of rings that do not
 * actually wrap (e.g. -17 in the Afro-Eurasia land ring) stay put. If the whole ring ends up entirely beyond
 * +/-180 it is shifted back into range. Pure; returns the input when no jump exists.
 *
 * @param ring Closed ring of [lon, lat, ...] positions.
 * @returns Ring with continuous longitudes.
 */
export function unwrapRing(ring: number[][]): number[][] {
  let offset = 0;
  const out = ring.map((point, i) => {
    const step = i > 0 ? point[0] - ring[i - 1][0] : 0;
    if (step > 180) offset -= 360;
    else if (step < -180) offset += 360;
    return [point[0] + offset, ...point.slice(1)];
  });
  if (offset === 0 && out.every((p, i) => p[0] === ring[i][0])) return ring;
  const lons = out.map((p) => p[0]);
  const shift = Math.min(...lons) > 180 ? -360 : Math.max(...lons) < -180 ? 360 : 0;
  return shift ? out.map(([lon, ...rest]) => [lon + shift, ...rest]) : out;
}

/** Convert bundled Natural Earth geometry with continuous antimeridian rings for the flat projection. */
function makeGeometry(object: 'land' | 'countries'): FeatureCollection {
  const collection = feature(worldTopology as unknown as Topology, object) as unknown as {
    features: { geometry: { type: string; coordinates: number[][][] | number[][][][] } }[];
  };
  for (const { geometry } of collection.features) {
    if (geometry.type === 'Polygon') {
      geometry.coordinates = (geometry.coordinates as number[][][]).map(unwrapRing);
    } else if (geometry.type === 'MultiPolygon') {
      geometry.coordinates = (geometry.coordinates as number[][][][]).map((poly) => poly.map(unwrapRing));
    }
  }
  return collection as unknown as FeatureCollection;
}

const landGeometry = makeGeometry('land');
const countryGeometry = makeGeometry('countries');

/**
 * Orthographic zoom that fits the whole world (360 x 180 degrees, Antarctica included) inside the canvas
 * with ~8% padding on the limiting axis. The view is centered on [0, 0], the mid lon/lat of the world.
 *
 * @param width Canvas width in px.
 * @param height Canvas height in px.
 * @returns log2 zoom for deck.gl's OrthographicView (1 unit = 1 degree).
 */
export function fitZoom(width: number, height: number): number {
  return Math.log2(Math.min(width / 360, height / 180) * 0.92);
}

/** Render an equirectangular map with optional bounded weather imagery and selectable evidence points. */
export function FlatMap({ features, selectedFeatureId, onSelectFeature, width, height, visible, interactive, weatherLayer, weatherWorkspaceId, weatherRevision, onWeatherStateChange, focusSelected = false }: FlatMapProps) {
  const t = useTranslations('dashboard');
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const contextHandlersRef = useRef<{ lost: (event: Event) => void; restored: () => void } | null>(null);
  const [contextLost, setContextLost] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [themeRevision, setThemeRevision] = useState(0);
  const weather = useWeatherRaster({ weatherLayer, weatherWorkspaceId, weatherRevision, onWeatherStateChange }, visible && !unavailable && !contextLost);

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeRevision((value) => value + 1));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => observer.disconnect();
  }, []);

  useEffect(() => () => {
    const canvas = canvasRef.current;
    const handlers = contextHandlersRef.current;
    if (canvas && handlers) {
      canvas.removeEventListener('webglcontextlost', handlers.lost);
      canvas.removeEventListener('webglcontextrestored', handlers.restored);
    }
    canvasRef.current = null;
    contextHandlersRef.current = null;
  }, []);

  const views = useMemo(() => [new OrthographicView({ id: 'world-flat', flipY: false })], []);
  const zoom = fitZoom(width, height);
  const [viewState, setViewState] = useState({ target: [0, 0, 0] as [number, number, number], zoom });
  // Resize changes the fitted external map view while preserving its current target.
  // eslint-disable-next-line react-hooks/set-state-in-effect -- syncs React state to the Deck-controlled view when the fitted zoom changes; the user's pan target must survive, so it cannot be derived or keyed.
  useEffect(() => { setViewState((current) => ({ ...current, zoom })); }, [zoom]);
  const focused = focusSelected ? features.find((item) => item.feature_id === selectedFeatureId) : undefined;
  const focusedLat = focused?.latitude, focusedLon = focused?.longitude;
  useEffect(() => {
    // Selection from the accessible event list must update Deck's controlled view.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- re-centres the Deck-controlled view only when the selected feature changes, so later user panning is not overwritten.
    if (focusedLat !== undefined && focusedLon !== undefined) setViewState((current) => ({ ...current, target: [focusedLon, focusedLat, 0] }));
  }, [selectedFeatureId, focusedLat, focusedLon]);
  const layers = useMemo(() => [
    new GeoJsonLayer({
      id: 'natural-earth-land',
      data: landGeometry,
      filled: true,
      stroked: true,
      pickable: false,
      getFillColor: readDeckColor('--active', '#e8ece8', 255),
      getLineColor: readDeckColor('--control', '#cfd5d0', 255),
      lineWidthMinPixels: 0.55,
    }),
    ...(weather ? [
      new BitmapLayer({ id: 'weather-raster', image: weather, bounds: [-180, -90, 180, 90], pickable: false }),
      new GeoJsonLayer({
        id: 'weather-land-boundaries', data: countryGeometry, filled: false, stroked: true, pickable: false,
        getLineColor: readDeckColor('--control', '#cfd5d0', 255), lineWidthMinPixels: 0.55,
      }),
    ] : []),
    new ScatterplotLayer<MapPoint>({
      id: 'world-evidence-points',
      data: features,
      pickable: true,
      radiusUnits: 'pixels',
      radiusMinPixels: 4,
      radiusMaxPixels: 10,
      getPosition: (item) => [item.longitude, item.latitude, 0],
      getRadius: (item) => item.feature_id === selectedFeatureId ? 7 : 5,
      getFillColor: (item) => readDeckColor(
        item.feature_id === selectedFeatureId ? '--accent' : '--chart-1', '#176a4a', 230,
      ),
      getLineColor: readDeckColor('--surface', '#ffffff', 255),
      lineWidthMinPixels: 1,
      updateTriggers: { getRadius: [selectedFeatureId], getFillColor: [selectedFeatureId, themeRevision] },
    }),
  ], [features, selectedFeatureId, themeRevision, weather]);

  /** Attach context lifecycle listeners to Deck's initialized canvas and retain them for cleanup. */
  const handleWebGLInitialized = (gl: WebGLRenderingContext) => {
    const canvas = gl.canvas as HTMLCanvasElement;
    const handlers = {
      lost: (event: Event) => {
        event.preventDefault();
        setContextLost(true);
      },
      restored: () => setContextLost(false),
    };
    const previousCanvas = canvasRef.current;
    const previousHandlers = contextHandlersRef.current;
    if (previousCanvas && previousHandlers) {
      previousCanvas.removeEventListener('webglcontextlost', previousHandlers.lost);
      previousCanvas.removeEventListener('webglcontextrestored', previousHandlers.restored);
    }
    canvas.addEventListener('webglcontextlost', handlers.lost);
    canvas.addEventListener('webglcontextrestored', handlers.restored);
    canvasRef.current = canvas;
    contextHandlersRef.current = handlers;
    setUnavailable(false);
  };

  return <div className="relative h-full min-h-48 shrink-0 w-full">
    <DeckGL<typeof views>
      views={views}
      viewState={{ 'world-flat': viewState }}
      onViewStateChange={({ viewState: next }) => setViewState(next as typeof viewState)}
      controller={visible && interactive}
      _animate={visible}
      layers={layers}
      onWebGLInitialized={handleWebGLInitialized}
      onError={() => setUnavailable(true)}
      onClick={(info) => {
        const item = info.object as MapPoint | undefined;
        if (item?.feature_id) onSelectFeature(item.feature_id);
      }}
      style={{ position: 'absolute', inset: '0' }}
      useDevicePixels={1}
    />
    {(unavailable || contextLost) && <p role="status" className="absolute left-2 top-2 rounded bg-card px-2 py-1 text-xs text-muted-foreground">{t('mapWebglUnavailable')}</p>}
  </div>;
}
