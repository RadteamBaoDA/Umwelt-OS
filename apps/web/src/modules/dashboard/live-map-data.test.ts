import { describe, expect, it } from 'vitest';
import { liveMapPoints, safeLiveLink, type LiveEvent } from './live-map-data';

const event: LiveEvent & { key: string } = { key: 'usgs:1', id: '1', category: 'geo', title: 'Event', observed_at: '2026-01-01T00:00:00Z', latitude: 0, longitude: 0, url: null, source: 'USGS', summary: null };
describe('live map provider data', () => {
  it('plots only finite coordinates within geographic bounds, including zero', () => {
    expect(liveMapPoints([event, { ...event, key: 'missing', latitude: null }, { ...event, key: 'invalid', longitude: 181 }, { ...event, key: 'nan', latitude: NaN }])).toEqual([{ feature_id: 'usgs:1', latitude: 0, longitude: 0 }]);
  });
  it('accepts web links only', () => {
    expect(safeLiveLink('https://example.com/event')).toBe('https://example.com/event');
    expect(safeLiveLink('javascript:alert(1)')).toBeNull();
    expect(safeLiveLink('/relative')).toBeNull();
  });
});
