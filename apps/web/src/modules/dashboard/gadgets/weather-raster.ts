'use client';

import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { workspaceGeneration, workspaceHeaders } from '../../../core/api';

/** The provider's supported weather overlays and the state exposed to map controls. */
export type WeatherLayer = 'temperature' | 'precipitation' | 'wind' | 'clouds';
export type WeatherState = 'idle' | 'loading' | 'ready' | 'error';
export type WeatherProps = {
  weatherLayer?: WeatherLayer | null;
  weatherWorkspaceId?: string;
  weatherRevision?: number;
  onWeatherStateChange?: (state: WeatherState) => void;
};

/**
 * Stitches the four 256px Web Mercator tiles (row-major) and reprojects them to a 512x256 equirectangular canvas.
 * Web Mercator has no pixels beyond 85.051 degrees, so the polar caps stay transparent.
 *
 * @param bitmaps Decoded tiles in row-major order.
 * @returns The reprojected canvas.
 * @throws Error when a 2D canvas context is unavailable.
 */
function reprojectToEquirectangular(bitmaps: ImageBitmap[]): HTMLCanvasElement {
  const mercator = document.createElement('canvas');
  mercator.width = mercator.height = 512;
  const source = mercator.getContext('2d');
  const canvas = document.createElement('canvas');
  canvas.width = 512;
  canvas.height = 256;
  const target = canvas.getContext('2d');
  if (!source || !target) throw new Error('Canvas unavailable');
  bitmaps.forEach((bitmap, index) => source.drawImage(bitmap, (index % 2) * 256, Math.floor(index / 2) * 256, 256, 256));
  // Web Mercator has no pixels beyond 85.051 degrees; leave polar caps transparent.
  for (let row = 0; row < canvas.height; row++) {
    const latitude = 90 - ((row + 0.5) * 180) / canvas.height;
    if (Math.abs(latitude) > 85.05112878) continue;
    const radians = (latitude * Math.PI) / 180;
    const sourceY = (1 - Math.asinh(Math.tan(radians)) / Math.PI) * 256;
    target.drawImage(mercator, 0, sourceY, 512, 1, 0, row, 512, 1);
  }
  return canvas;
}

/** Weather refresh period; the upstream tiles change slowly. */
export const WEATHER_REFRESH_MS = 600_000;

/**
 * Load four same-origin Mercator tiles at zoom 1, then resample them into a bounded
 * equirectangular image for both map engines. Results are memory-only and aborted
 * when the selection, workspace, revision, or visibility changes; refreshes every 10 minutes while visible. A refresh keeps the previous raster until the new one is ready.
 * Failed sets are never shown.
 */
export function useWeatherRaster(
  { weatherLayer, weatherWorkspaceId, weatherRevision, onWeatherStateChange }: WeatherProps,
  visible: boolean,
): HTMLCanvasElement | null {
  const [result, setResult] = useState<{ key: string; generation: number; canvas: HTMLCanvasElement } | null>(null);
  const [pageVisible, setPageVisible] = useState(() => typeof document === 'undefined' || document.visibilityState === 'visible');
  const [refresh, setRefresh] = useState(0);
  // Key whose raster was last published; a refresh of it keeps showing the old canvas and stays 'ready'.
  const readyKeyRef = useRef<string | null>(null);
  const callback = useRef(onWeatherStateChange);
  useLayoutEffect(() => {
    callback.current = onWeatherStateChange;
  }, [onWeatherStateChange]);
  const key =
    visible && pageVisible && weatherLayer && weatherWorkspaceId ? `${weatherWorkspaceId}:${weatherLayer}:${weatherRevision ?? 0}` : null;

  useEffect(() => {
    /** Follow browser visibility so hidden pages stop requesting tiles. */
    const update = () => setPageVisible(document.visibilityState === 'visible');
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  }, []);

  useEffect(() => {
    if (!key) return;
    const timer = window.setInterval(() => setRefresh((value) => value + 1), WEATHER_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [key]);

  useEffect(() => {
    if (!key || !weatherLayer || !weatherWorkspaceId) {
      callback.current?.('idle');
      return;
    }
    const generation = workspaceGeneration();
    const selected = new Headers(workspaceHeaders('selected')).get('X-Workspace-ID');
    if (selected !== weatherWorkspaceId || workspaceGeneration() !== generation) {
      callback.current?.('idle');
      return;
    }
    const abort = new AbortController();
    // A key change hides the old raster by itself (the returned value is key-checked); a plain refresh keeps it
    // until the new tiles are ready, so the overlay never blanks.
    const refreshing = readyKeyRef.current === key;
    if (!refreshing) callback.current?.('loading');
    /** Fetch and reproject four bounded tiles, discarding results when authority or visibility changes. */
    const load = async () => {
      const bitmaps: ImageBitmap[] = [];
      try {
        // Keep the four requests bounded and release decoded image memory even on partial failure.
        for (let y = 0; y < 2; y++) {
          for (let x = 0; x < 2; x++) {
            if (workspaceGeneration() !== generation) {
              abort.abort();
              return;
            }
            const response = await fetch(`/api/v1/live-map/weather/${weatherLayer}/1/${x}/${y}`, {
              credentials: 'same-origin',
              cache: 'no-store',
              signal: abort.signal,
              headers: { 'X-Workspace-ID': weatherWorkspaceId },
            });
            if (!response.ok) throw new Error(`Weather tile: ${response.status}`);
            const bitmap = await createImageBitmap(await response.blob());
            bitmaps.push(bitmap);
            if (abort.signal.aborted) return;
          }
        }
        const canvas = reprojectToEquirectangular(bitmaps);
        // A completed response must not republish data after the selected workspace changes.
        if (!abort.signal.aborted && workspaceGeneration() !== generation) abort.abort();
        if (!abort.signal.aborted) {
          setResult({ key, generation, canvas });
          readyKeyRef.current = key;
          callback.current?.('ready');
        }
      } catch {
        if (!abort.signal.aborted && workspaceGeneration() === generation && !refreshing) callback.current?.('error');
      } finally {
        bitmaps.forEach((bitmap) => bitmap.close());
      }
    };
    void load();
    return () => abort.abort();
  }, [key, refresh, weatherLayer, weatherWorkspaceId]);

  return result?.key === key &&
    result.generation === workspaceGeneration() &&
    new Headers(workspaceHeaders('selected')).get('X-Workspace-ID') === weatherWorkspaceId
    ? result.canvas
    : null;
}
