'use client';

import { DeckGL } from '@deck.gl/react';
import { GeoJsonLayer, ScatterplotLayer } from '@deck.gl/layers';
import { OrthographicView } from '@deck.gl/core';
import { feature } from 'topojson-client';
import worldTopology from 'world-atlas/countries-110m.json';
import type { Topology } from 'topojson-specification';
import { useTranslations } from 'next-intl';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { MapFeature } from '../map-layers';

/** Props shared by the accessible map controller and local deck.gl renderer. */
export type FlatMapProps = {
  features: MapFeature[];
  selectedFeatureId: string | null;
  onSelectFeature: (featureId: string) => void;
  width: number;
  height: number;
  visible: boolean;
  interactive: boolean;
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

/** Convert the bundled Natural Earth topology once; flat rendering does not fetch tiles or a basemap. */
const landGeometry = feature(worldTopology as unknown as Topology, 'land');

/** Render a local equirectangular plot whose 192px floor does not shrink beneath map controls. */
export function FlatMap({ features, selectedFeatureId, onSelectFeature, width, height, visible, interactive }: FlatMapProps) {
  const t = useTranslations('dashboard');
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const contextHandlersRef = useRef<{ lost: (event: Event) => void; restored: () => void } | null>(null);
  const [contextLost, setContextLost] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [themeRevision, setThemeRevision] = useState(0);

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

  const views = useMemo(() => [new OrthographicView({ id: 'world-flat' })], []);
  const zoom = Math.log2(Math.max(1, Math.min(width / 360, height / 180)));
  const layers = useMemo(() => [
    new GeoJsonLayer({
      id: 'natural-earth-land',
      data: landGeometry,
      filled: true,
      stroked: true,
      pickable: false,
      getFillColor: readDeckColor('--bg', '#e5e7eb', 180),
      getLineColor: readDeckColor('--line', '#a8b0ba', 220),
      lineWidthMinPixels: 0.55,
    }),
    new ScatterplotLayer<MapFeature>({
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
  ], [features, selectedFeatureId, themeRevision]);

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
      viewState={{ 'world-flat': { target: [0, 0, 0], zoom } }}
      controller={visible && interactive}
      _animate={visible}
      layers={layers}
      onWebGLInitialized={handleWebGLInitialized}
      onError={() => setUnavailable(true)}
      onClick={(info) => {
        const item = info.object as MapFeature | undefined;
        if (item?.feature_id) onSelectFeature(item.feature_id);
      }}
      style={{ position: 'absolute', inset: '0' }}
      useDevicePixels={1}
    />
    {(unavailable || contextLost) && <p role="status" className="absolute left-2 top-2 rounded bg-card px-2 py-1 text-xs text-muted-foreground">{t('mapWebglUnavailable')}</p>}
  </div>;
}
