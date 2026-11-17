import { describe, expect, it } from 'vitest';
import { unsupportedListFilters } from './api';

describe('unsupportedListFilters', () => {
  it('ignores server default map filters', () => {
    expect(unsupportedListFilters({ keywords: ['a'], limit: 5, map_engine: null, show_precise_locations: false })).toEqual([]);
  });
  it('reports map filters that hold a real value', () => {
    expect(unsupportedListFilters({ map_engine: 'globe', show_precise_locations: true })).toEqual(['map_engine', 'show_precise_locations']);
  });
});
