'use client';

import 'maplibre-gl/dist/maplibre-gl.css';
import { Map as MapLibreMap, type IControl } from 'maplibre-gl';
import { MapboxOverlay } from '@deck.gl/mapbox';
import { COORDINATE_SYSTEM } from '@deck.gl/core';
import { BitmapLayer, ScatterplotLayer } from '@deck.gl/layers';
import { useTranslations } from 'next-intl';
import { useEffect, useMemo, useRef, useState } from 'react';
import { centroidView, fitWorldZoom, locationKey, shouldRecentre } from './globe-view';
import { useWeatherRaster, type WeatherProps } from './weather-raster';

/** Coordinates and stable identity shared by evidence and live-event map points. */
export type MapPoint = {
  feature_id: string;
  latitude: number;
  longitude: number;
  metric?: string;
  observed_at?: string;
};

/** Props of the single MapLibre + deck.gl renderer. `projection` is derived from the saved `map_engine` by the callers. */
export type WorldMapProps = WeatherProps & {
  features: MapPoint[];
  selectedFeatureId: string | null;
  onSelectFeature: (featureId: string) => void;
  width: number;
  height: number;
  visible: boolean;
  interactive: boolean;
  projection: 'globe' | 'mercator';
  focusSelected?: boolean;
};

/** OpenFreeMap styles (no key, OSM data); the browser fetches them directly, see core/csp.ts. */
const STYLE_LIGHT = 'https://tiles.openfreemap.org/styles/positron';
const STYLE_DARK = 'https://tiles.openfreemap.org/styles/dark';

/** True when the app theme class on <html> is dark (next-themes toggles it for light, dark and system). */
function isDarkTheme(): boolean {
  return typeof document !== 'undefined' && document.documentElement.classList.contains('dark');
}

/** Convert the active semantic CSS token to deck.gl's numeric colour contract (hex tokens only; fallback is the accent green). */
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

/** Throw when the browser cannot create a WebGL context, so the caller can show the unavailable note instead of a blank plot. */
function assertWebGLAvailable(): void {
  const probe = document.createElement('canvas');
  if (!probe.getContext('webgl2') && !probe.getContext('webgl')) throw new Error('WebGL is unavailable');
}

/**
 * Enable or disable every user gesture of the map at once (edit mode and hidden gadgets must not capture input).
 * MapLibre only accepts `interactive` at construction, so the individual handlers are toggled instead.
 */
function setMapInteractive(map: MapLibreMap, on: boolean): void {
  const handlers = [map.dragPan, map.dragRotate, map.scrollZoom, map.boxZoom, map.keyboard, map.doubleClickZoom, map.touchZoomRotate, map.touchPitch];
  for (const handler of handlers) { if (on) handler.enable(); else handler.disable(); }
}

/**
 * Tracks WebGL context loss of the map canvas: loss is prevented from destroying the map and reported until restored.
 *
 * @returns Cleanup removing both listeners.
 */
function watchContext(map: MapLibreMap, setContextLost: (lost: boolean) => void): () => void {
  const canvas = map.getCanvas();
  const lost = (event: Event) => { event.preventDefault(); setContextLost(true); };
  const restored = () => setContextLost(false);
  canvas.addEventListener('webglcontextlost', lost);
  canvas.addEventListener('webglcontextrestored', restored);
  return () => { canvas.removeEventListener('webglcontextlost', lost); canvas.removeEventListener('webglcontextrestored', restored); };
}

/**
 * Builds the deck.gl layers: the optional equirectangular weather raster and the selectable evidence points
 * (selected highlight via semantic tokens, re-read when `themeRevision` changes).
 */
function useWorldMapLayers(features: MapPoint[], selectedFeatureId: string | null, themeRevision: number, weather: HTMLCanvasElement | null) {
  return useMemo(() => [
    ...(weather ? [new BitmapLayer({
      id: 'weather-raster', image: weather, bounds: [-180, -90, 180, 90], pickable: false,
      // The raster is equirectangular (see weather-raster.ts); the layer reprojects it for mercator and globe.
      _imageCoordinateSystem: COORDINATE_SYSTEM.LNGLAT,
    })] : []),
    new ScatterplotLayer<MapPoint>({
      id: 'world-evidence-points',
      data: features,
      pickable: true,
      radiusUnits: 'pixels',
      radiusMinPixels: 4,
      radiusMaxPixels: 10,
      getPosition: (item) => [item.longitude, item.latitude, 0],
      getRadius: (item) => item.feature_id === selectedFeatureId ? 7 : 5,
      getFillColor: (item) => readDeckColor(item.feature_id === selectedFeatureId ? '--accent' : '--chart-1', '#176a4a', 230),
      getLineColor: readDeckColor('--surface', '#ffffff', 255),
      lineWidthMinPixels: 1,
      updateTriggers: { getRadius: [selectedFeatureId], getFillColor: [selectedFeatureId, themeRevision] },
    }),
  ], [features, selectedFeatureId, themeRevision, weather]);
}

/**
 * Single dashboard map renderer: an OpenFreeMap basemap in MapLibre GL (globe or Mercator projection) with deck.gl
 * layers interleaved through MapboxOverlay (selectable evidence points and the optional same-origin weather raster).
 * Follows the light/dark theme at runtime, disables gestures when hidden or in edit mode, and shows a status note
 * when WebGL is unavailable or its context is lost. Selection is reported through `onSelectFeature` only.
 */
export function WorldMap({ features, selectedFeatureId, onSelectFeature, width, height, visible, interactive, projection, weatherLayer, weatherWorkspaceId, weatherRevision, onWeatherStateChange, focusSelected = false }: WorldMapProps) {
  const t = useTranslations('dashboard');
  const hostRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);
  const [ready, setReady] = useState(false), [unavailable, setUnavailable] = useState(false), [contextLost, setContextLost] = useState(false);
  const [dark, setDark] = useState(isDarkTheme);
  const [themeRevision, setThemeRevision] = useState(0);
  // Latest values for the one-time map construction and the click handler, so neither forces a rebuild.
  const latest = useRef({ features, projection, width, height, visible, interactive, onSelectFeature });
  useEffect(() => { latest.current = { features, projection, width, height, visible, interactive, onSelectFeature }; });
  // Location-set key the view was last centred on; refreshes of the same places must not undo the user's pan/zoom.
  const centredKeyRef = useRef<string | null>(null);
  const isSized = width > 0 && height > 0, failed = unavailable || contextLost;
  const weather = useWeatherRaster({ weatherLayer, weatherWorkspaceId, weatherRevision, onWeatherStateChange: failed ? undefined : onWeatherStateChange }, visible && !failed && isSized);

  useEffect(() => {
    /** Re-read theme colours when next-themes flips the class on <html>. */
    const observer = new MutationObserver(() => { setDark(isDarkTheme()); setThemeRevision((value) => value + 1); });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => observer.disconnect();
  }, []);

  /** Build the map once the plot is measurable; removed (context released) on unmount or when it becomes unmeasurable. */
  useEffect(() => {
    const host = hostRef.current;
    if (!host || !isSized) return;
    try {
      assertWebGLAvailable();
      const initial = latest.current;
      const view = centroidView(initial.features);
      const map = new MapLibreMap({
        container: host,
        style: isDarkTheme() ? STYLE_DARK : STYLE_LIGHT,
        center: initial.projection === 'globe' && view ? [view.lng, view.lat] : [0, 0],
        zoom: fitWorldZoom(initial.width, initial.height, initial.projection),
        attributionControl: { compact: true },
        // The component calls map.resize() itself when width/height change, so the built-in observer is redundant.
        trackResize: false,
      });
      // Interleaved: deck layers draw inside MapLibre's WebGL context, so globe projection and depth stay consistent.
      const overlay = new MapboxOverlay({
        interleaved: true,
        onClick: (info) => {
          const item = info.object as MapPoint | undefined;
          if (item?.feature_id) latest.current.onSelectFeature(item.feature_id);
        },
      });
      map.addControl(overlay as unknown as IControl);
      // A style load (first load and every theme switch) resets the projection to the style's own; re-apply the requested one.
      map.on('style.load', () => map.setProjection({ type: latest.current.projection }));
      setMapInteractive(map, initial.visible && initial.interactive);
      centredKeyRef.current = view ? locationKey(initial.features) : null;
      const stopWatching = watchContext(map, setContextLost);
      // Map creation is an external-system sync; clearing the failure flag and signalling readiness belong here.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setUnavailable(false);
      mapRef.current = map;
      overlayRef.current = overlay;
      setReady(true);
      return () => {
        stopWatching();
        mapRef.current = null;
        overlayRef.current = null;
        setReady(false);
        map.remove();
      };
    } catch (error) {
      console.warn('Map renderer unavailable:', error instanceof Error ? error.message : error);
      setUnavailable(true);
    }
  }, [isSized]);

  /** Follow the theme with the matching OpenFreeMap style; the overlay re-attaches deck layers on style change. */
  const appliedDark = useRef(dark);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || appliedDark.current === dark) return;
    appliedDark.current = dark;
    map.setStyle(dark ? STYLE_DARK : STYLE_LIGHT);
  }, [dark, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (map?.isStyleLoaded()) map.setProjection({ type: projection });
  }, [projection, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.resize();
    // MapLibre renders on demand (no loop to pause), so hiding only needs gestures off.
    setMapInteractive(map, visible && interactive);
  }, [width, height, visible, interactive, ready]);

  const layers = useWorldMapLayers(features, selectedFeatureId, themeRevision, weather);
  useEffect(() => { overlayRef.current?.setProps({ layers }); }, [layers, ready]);

  const focused = focusSelected ? features.find((item) => item.feature_id === selectedFeatureId) : undefined;
  const focusedLat = focused?.latitude, focusedLon = focused?.longitude;
  const hasFocus = focusedLat !== undefined && focusedLon !== undefined;
  useEffect(() => {
    // Selection from the accessible event list moves the view; later user panning is not overwritten.
    if (focusedLat !== undefined && focusedLon !== undefined) mapRef.current?.easeTo({ center: [focusedLon, focusedLat], duration: 500 });
  }, [selectedFeatureId, focusedLat, focusedLon, ready]);

  /** Globe only: recentre on the evidence centroid when the set of locations changes (data arrives late, places added/removed). */
  useEffect(() => {
    const key = locationKey(features);
    if (projection !== 'globe' || hasFocus || !shouldRecentre(centredKeyRef.current, key)) return;
    const view = centroidView(features);
    if (!view || !mapRef.current) return;
    mapRef.current.jumpTo({ center: [view.lng, view.lat] });
    centredKeyRef.current = key;
  }, [features, hasFocus, projection, ready]);

  return <div className="relative h-full min-h-48 shrink-0 w-full">
    <div ref={hostRef} className={failed ? 'hidden' : 'absolute inset-0'} />
    {failed && <p role="status" className="absolute left-2 top-2 rounded bg-card px-2 py-1 text-xs text-muted-foreground">{t('mapWebglUnavailable')}</p>}
  </div>;
}
