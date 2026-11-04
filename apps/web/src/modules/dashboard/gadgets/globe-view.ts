/**
 * Stable identity of a set of point locations: coordinates rounded to 0.1 degree, de-duplicated and sorted.
 * Value or timestamp refreshes of the same places yield the same key, so the globe keeps the user's view.
 *
 * @param points Points with WGS84 coordinates.
 * @returns Key string; empty when there are no points.
 */
export function locationKey(points: Array<{ latitude: number; longitude: number }>): string {
  return [...new Set(points.map((p) => `${p.latitude.toFixed(1)},${p.longitude.toFixed(1)}`))].sort().join('|');
}

/**
 * Decides whether the globe should recentre: only for a non-empty location set that differs from the one
 * last centred on. A user's rotation/zoom is therefore preserved until the places themselves change.
 *
 * @param previousKey Key of the last centred location set, or null before the first centring.
 * @param nextKey Key of the incoming location set.
 * @returns True when the view should move to the new centroid.
 */
export function shouldRecentre(previousKey: string | null, nextKey: string): boolean {
  return nextKey !== '' && nextKey !== previousKey;
}
