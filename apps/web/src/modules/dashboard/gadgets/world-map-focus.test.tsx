import { render, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ easeTo: vi.fn(), instances: 0 }));
vi.mock('maplibre-gl/dist/maplibre-gl.css', () => ({}));
vi.mock('maplibre-gl', () => {
  const handler = { enable: vi.fn(), disable: vi.fn() };
  class Map {
    dragPan = handler; dragRotate = handler; scrollZoom = handler; boxZoom = handler; keyboard = handler;
    doubleClickZoom = handler; touchZoomRotate = handler; touchPitch = handler;
    easeTo = state.easeTo;
    constructor() { state.instances += 1; }
    addControl() {}
    on() {}
    isStyleLoaded() { return false; }
    getCanvas() { return document.createElement('canvas'); }
    setProjection() {}
    setStyle() {}
    resize() {}
    jumpTo() {}
    remove() {}
  }
  return { Map };
});
vi.mock('@deck.gl/mapbox', () => ({ MapboxOverlay: class { setProps() {} } }));
vi.mock('@deck.gl/core', () => ({ COORDINATE_SYSTEM: { LNGLAT: 1 } }));
vi.mock('@deck.gl/layers', () => ({ BitmapLayer: class {}, ScatterplotLayer: class {} }));
vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => key }));
vi.mock('./weather-raster', () => ({ useWeatherRaster: () => null }));
import { WorldMap } from './world-map';

describe('world map focus', () => {
  it('eases to a selected geographic event only when focusSelected is set', async () => {
    HTMLCanvasElement.prototype.getContext = (() => ({})) as never;
    const props = { features: [{ feature_id: 'event', latitude: 21, longitude: 105 }], selectedFeatureId: null as string | null, onSelectFeature: () => {}, width: 720, height: 360, visible: true, interactive: true, projection: 'mercator' as const };
    const view = render(<WorldMap {...props} focusSelected />);
    view.rerender(<WorldMap {...props} focusSelected selectedFeatureId="event" />);
    await waitFor(() => expect(state.easeTo).toHaveBeenCalledWith({ center: [105, 21], duration: 500 }));
    state.easeTo.mockClear();
    view.unmount();
    render(<WorldMap {...props} selectedFeatureId="event" />);
    expect(state.easeTo).not.toHaveBeenCalled();
  });
});
