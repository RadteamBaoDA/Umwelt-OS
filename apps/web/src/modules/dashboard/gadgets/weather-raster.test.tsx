import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('@/core/api', () => ({ workspaceGeneration: () => 1, workspaceHeaders: () => ({ 'X-Workspace-ID': 'workspace' }) }));
import { useWeatherRaster } from './weather-raster';

afterEach(() => vi.unstubAllGlobals());

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
