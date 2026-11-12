import { describe, expect, it } from 'vitest';
import { centroidView, fitWorldZoom, locationKey, shouldRecentre } from './globe-view';

describe('globe view stability', () => {
  const a = [{ latitude: 10, longitude: 20 }, { latitude: -5.04, longitude: 100 }];
  it('keeps the same key across order and sub-0.1-degree jitter', () => {
    expect(locationKey([a[1], a[0]])).toBe(locationKey([{ latitude: 10.01, longitude: 20 }, a[1]]));
  });
  it('recentres on first data and on a changed location set only', () => {
    expect(shouldRecentre(null, locationKey(a))).toBe(true);
    expect(shouldRecentre(locationKey(a), locationKey(a))).toBe(false);
    expect(shouldRecentre(locationKey(a), locationKey([...a, { latitude: 1, longitude: 1 }]))).toBe(true);
    expect(shouldRecentre(locationKey(a), '')).toBe(false);
  });
});

describe('centroidView / fitWorldZoom', () => {
  it('averages across the antimeridian and handles empty input', () => {
    expect(centroidView([])).toBeNull();
    const view = centroidView([{ latitude: 0, longitude: 179 }, { latitude: 0, longitude: -179 }])!;
    expect(Math.abs(view.lng)).toBeCloseTo(180, 5);
  });
  it('fits the world without going below zoom 0', () => {
    expect(fitWorldZoom(1024, 1024, 'mercator')).toBeGreaterThan(fitWorldZoom(256, 256, 'mercator'));
    expect(fitWorldZoom(10, 10, 'globe')).toBe(0);
  });
});
