import { describe, expect, it, vi } from 'vitest';

// deck.gl needs WebGL; only the pure ring helper is under test.
vi.mock('@deck.gl/react', () => ({ DeckGL: () => null }));
vi.mock('@deck.gl/layers', () => ({ GeoJsonLayer: class {}, ScatterplotLayer: class {} }));
vi.mock('@deck.gl/core', () => ({ OrthographicView: class {} }));
import { unwrapRing } from './flat-map';

const lons = (ring: number[][]) => ring.map((p) => p[0]);

describe('unwrapRing', () => {
  it('leaves a ring that does not cross the antimeridian unchanged', () => {
    const ring = [[-17, 10], [30, 40], [100, 20], [-17, 10]];
    expect(unwrapRing(ring)).toBe(ring);
  });

  it('keeps a ring that crosses 180 continuous', () => {
    const out = unwrapRing([[170, 0], [179, 1], [-179, 2], [-170, 3], [170, 0]]);
    expect(lons(out)).toEqual([170, 179, 181, 190, 170]);
  });

  it('keeps western longitudes of an Afro-Eurasia-like ring at their value', () => {
    const out = unwrapRing([[-17, 14], [30, 40], [180, 65], [-170, 66]]);
    expect(out[0][0]).toBe(-17);
    expect(lons(out)).toEqual([-17, 30, 180, 190]);
    expect(lons(out).every((v, i, a) => i === 0 || Math.abs(v - a[i - 1]) <= 180)).toBe(true);
  });
});
