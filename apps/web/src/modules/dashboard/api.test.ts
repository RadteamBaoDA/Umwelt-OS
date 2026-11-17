import { describe, expect, it } from 'vitest';
import { unsupportedListFilters } from './api';

describe('unsupportedListFilters', () => {
  it('ignores server default map and media filters', () => {
    expect(
      unsupportedListFilters({
        keywords: ['a'],
        limit: 5,
        map_engine: null,
        show_precise_locations: false,
        media_type: null,
        media_types: [],
        release_status: null,
        group_by: null,
        countries: [],
        genres: [],
        languages: [],
        formats: [],
        content_ratings: [],
        release_year: null,
        release_season: null,
        min_rating: null,
      }),
    ).toEqual([]);
  });
  it('reports map and non-default media filters including min_rating 0', () => {
    expect(unsupportedListFilters({ map_engine: 'globe', show_precise_locations: true })).toEqual(['map_engine', 'show_precise_locations']);
    expect(
      unsupportedListFilters({
        media_type: 'anime',
        release_status: 'airing',
        group_by: 'date',
        countries: ['JP'],
        genres: ['sci_fi'],
        min_rating: 0,
      }),
    ).toEqual(['media_type', 'release_status', 'group_by', 'countries', 'genres', 'min_rating']);
  });
});
