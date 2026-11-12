/** Event fields returned by the live proxy; coordinates are optional provider facts. */
export type LiveEvent = {
  id: string;
  category: string;
  title: string;
  observed_at: string | null;
  latitude: number | null;
  longitude: number | null;
  url: string | null;
  source: string;
  summary: string | null;
  sources?: { id: string | null; url: string }[];
  tags?: string[];
};

/** Reject executable and local URL schemes supplied by remote providers. */
export function safeLiveLink(value: string | null): string | null {
  try {
    const url = new URL(value ?? '');
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
  } catch {
    return null;
  }
}

/** Project only finite provider coordinates inside geographic bounds, preserving the controller's event key. */
export function liveMapPoints(events: (LiveEvent & { key: string })[]) {
  return events
    .filter(
      (event) =>
        typeof event.latitude === 'number' &&
        Number.isFinite(event.latitude) &&
        Math.abs(event.latitude) <= 90 &&
        typeof event.longitude === 'number' &&
        Number.isFinite(event.longitude) &&
        Math.abs(event.longitude) <= 180,
    )
    .map((event) => ({ feature_id: event.key, latitude: event.latitude as number, longitude: event.longitude as number }));
}
