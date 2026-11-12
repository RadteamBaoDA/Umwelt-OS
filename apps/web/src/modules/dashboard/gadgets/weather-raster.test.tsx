import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('@/core/api', () => ({ workspaceGeneration: () => 1, workspaceHeaders: () => ({ 'X-Workspace-ID': 'workspace' }) }));
import { useWeatherRaster, WEATHER_REFRESH_MS } from './weather-raster';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it('aborts the tile request when its gadget becomes hidden', async () => {
  let signal: AbortSignal | undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn(
      (_url, options: RequestInit) =>
        new Promise((_resolve, reject) => {
          signal = options.signal as AbortSignal;
          signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }),
    ),
  );
  const { rerender } = renderHook(
    ({ visible }) => useWeatherRaster({ weatherLayer: 'wind', weatherWorkspaceId: 'workspace', weatherRevision: 1 }, visible),
    { initialProps: { visible: true } },
  );
  await waitFor(() => expect(signal).toBeDefined());
  rerender({ visible: false });
  expect(signal?.aborted).toBe(true);
});

it('reprojects four tiles across the globe and leaves polar caps transparent', async () => {
  const draws = vi.fn();
  const closes = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: draws } as unknown as CanvasRenderingContext2D);
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, blob: async () => new Blob() })),
  );
  vi.stubGlobal(
    'createImageBitmap',
    vi.fn(async () => ({ close: closes })),
  );
  const { result } = renderHook(() =>
    useWeatherRaster({ weatherLayer: 'wind', weatherWorkspaceId: 'workspace', weatherRevision: 1 }, true),
  );
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

it('keeps the previous raster while a refresh is in flight', async () => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
  vi.stubGlobal(
    'createImageBitmap',
    vi.fn(async () => ({ close: vi.fn() })),
  );
  let hang = false;
  vi.stubGlobal(
    'fetch',
    vi.fn((_url, options: RequestInit) =>
      hang
        ? new Promise((_resolve, reject) =>
            (options.signal as AbortSignal).addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))),
          )
        : Promise.resolve({ ok: true, blob: async () => new Blob() }),
    ),
  );
  // Capture the refresh tick instead of faking timers (waitFor itself polls with setInterval).
  const tick = { run: () => {} };
  const realSetInterval = window.setInterval.bind(window);
  vi.spyOn(window, 'setInterval').mockImplementation(((handler: TimerHandler, delay?: number) => {
    if (delay === WEATHER_REFRESH_MS) {
      tick.run = handler as () => void;
      return 0;
    }
    return realSetInterval(handler, delay);
  }) as typeof window.setInterval);
  {
    const states: string[] = [];
    const { result } = renderHook(() =>
      useWeatherRaster(
        { weatherLayer: 'wind', weatherWorkspaceId: 'workspace', weatherRevision: 1, onWeatherStateChange: (state) => states.push(state) },
        true,
      ),
    );
    await waitFor(() => expect(result.current).not.toBeNull());
    const first = result.current;
    hang = true;
    await act(async () => {
      tick.run();
    });
    expect(fetch).toHaveBeenCalledTimes(5);
    expect(result.current).toBe(first);
    expect(states).toEqual(['loading', 'ready']);
  }
});
