import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('@/core/api', () => ({ workspaceGeneration: () => 1, workspaceHeaders: () => ({ 'X-Workspace-ID': 'workspace' }) }));
import { useWeatherRaster } from './weather-raster';

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it('aborts the tile request when its gadget becomes hidden', async () => {
  let signal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn((_url, options: RequestInit) => new Promise((_resolve, reject) => {
    signal = options.signal as AbortSignal;
    signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
  })));
  const { rerender } = renderHook(({ visible }) => useWeatherRaster({ weatherLayer: 'wind', weatherWorkspaceId: 'workspace', weatherRevision: 1 }, visible), { initialProps: { visible: true } });
  await waitFor(() => expect(signal).toBeDefined());
  rerender({ visible: false });
  expect(signal?.aborted).toBe(true);
});


it('reprojects four tiles across the globe and leaves polar caps transparent', async () => {
  const draws = vi.fn();
  const closes = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: draws } as unknown as CanvasRenderingContext2D);
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, blob: async () => new Blob() })));
  vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ close: closes })));
  const { result } = renderHook(() => useWeatherRaster({ weatherLayer: 'wind', weatherWorkspaceId: 'workspace', weatherRevision: 1 }, true));
  await waitFor(() => expect(result.current).not.toBeNull());
  expect(fetch).toHaveBeenCalledTimes(4);
  expect(closes).toHaveBeenCalledTimes(4);
  expect(result.current?.width).toBe(512);
  expect(result.current?.height).toBe(256);
  const scanlines = draws.mock.calls.filter((call) => call.length === 9);
  expect(scanlines.some((call) => call[6] === 0 || call[6] === 255)).toBe(false);
  const equator = scanlines.find((call) => call[6] === 127);
  expect(equator?.[2]).toBeGreaterThan(254);
  expect(equator?.[2]).toBeLessThan(258);
});
