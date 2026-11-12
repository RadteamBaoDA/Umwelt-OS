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

/**
 * Centroid of the evidence points as a lng/lat view; averaging unit vectors keeps the result correct across the antimeridian.
 *
 * @param points Map points with WGS84 coordinates.
 * @returns lat/lng in degrees, or null when there are no points (callers keep the default view).
 */
export function centroidView(points: Array<{ latitude: number; longitude: number }>): { lat: number; lng: number } | null {
  if (!points.length) return null;
  const rad = Math.PI / 180;
  let x = 0, y = 0, z = 0;
  for (const p of points) {
    x += Math.cos(p.latitude * rad) * Math.cos(p.longitude * rad);
    y += Math.cos(p.latitude * rad) * Math.sin(p.longitude * rad);
    z += Math.sin(p.latitude * rad);
  }
  if (Math.hypot(x, y, z) < 1e-6) return null;
  return { lat: Math.atan2(z, Math.hypot(x, y)) / rad, lng: Math.atan2(y, x) / rad };
}

/**
 * MapLibre zoom that fits the whole world in a plot with ~10% padding on the limiting axis.
 * MapLibre's world is 512px wide at zoom 0; the globe diameter is worldSize/pi, the Mercator world is worldSize wide.
 *
 * @param width Plot width in px.
 * @param height Plot height in px.
 * @param projection Map projection in use.
 * @returns Zoom level (fractional, never below 0).
 */
export function fitWorldZoom(width: number, height: number, projection: 'globe' | 'mercator'): number {
  const side = projection === 'globe' ? Math.min(width, height) * 0.9 * Math.PI : Math.min(width, height * 2) * 0.9;
  return Math.max(0, Math.log2(side / 512));
}
