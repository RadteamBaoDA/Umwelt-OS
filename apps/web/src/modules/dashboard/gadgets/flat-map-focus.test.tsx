import { render, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ view: null as null | { target: number[] } }));
vi.mock('@deck.gl/react', () => ({ DeckGL: ({ viewState }: { viewState: { 'world-flat': { target: number[] } } }) => { state.view = viewState['world-flat']; return null; } }));
vi.mock('@deck.gl/layers', () => ({ BitmapLayer: class {}, GeoJsonLayer: class {}, ScatterplotLayer: class {} }));
vi.mock('@deck.gl/core', () => ({ OrthographicView: class {} }));
vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => key }));
vi.mock('./weather-raster', () => ({ useWeatherRaster: () => null }));
import { FlatMap } from './flat-map';

describe('live map focus', () => {
  it('centers a selected geographic event and keeps old maps at their own view', async () => {
    const props = { features: [{ feature_id: 'event', latitude: 21, longitude: 105 }], selectedFeatureId: null as string | null, onSelectFeature: () => {}, width: 720, height: 360, visible: true, interactive: true };
    const view = render(<FlatMap {...props} focusSelected />);
    view.rerender(<FlatMap {...props} focusSelected selectedFeatureId="event" />);
    await waitFor(() => expect(state.view?.target).toEqual([105, 21, 0]));
    view.unmount();
    render(<FlatMap {...props} selectedFeatureId="event" />);
    expect(state.view?.target).toEqual([0, 0, 0]);
  });
});
