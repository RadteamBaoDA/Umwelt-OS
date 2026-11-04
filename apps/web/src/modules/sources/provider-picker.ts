import type { ConnectorCatalogEntry } from './api';

/** Lowercases and strips combining marks (plus Vietnamese đ) so "Tin tuc" matches "Tin tức". */
export function foldSearchText(value: string): string {
  return value.toLowerCase().normalize('NFD').replace(/\p{M}/gu, '').replace(/đ/g, 'd');
}

/** One display panel of provider tiles; `key` is the category id or `nokey` for the leading no-key group. */
export interface ProviderTileGroup {
  key: string;
  entries: ConnectorCatalogEntry[];
}

/**
 * Builds the provider-picker panels: a leading "nokey" group holding every provider that needs no credentials,
 * then the category panels in `order` holding only credential-requiring providers, which therefore sort last overall.
 * Pure: filters by `query` (case- and diacritic-insensitive) against the tile label and its translated category name.
 *
 * @param entries Catalog entries already stripped of group rows.
 * @param order Category ids in display order; uncategorised entries use `other`.
 * @param categoryLabel Resolves a category id to its localized name, searched alongside the tile label.
 * @param query Raw search text; blank keeps every entry.
 * @returns Non-empty groups only.
 */
export function buildProviderGroups(
  entries: ConnectorCatalogEntry[],
  order: readonly string[],
  categoryLabel: (category: string) => string,
  query: string,
): ProviderTileGroup[] {
  const needle = foldSearchText(query.trim());
  /** True when the entry label or category label contains the folded search text (everything matches an empty query). */
  const matches = (entry: ConnectorCatalogEntry) => !needle
    || foldSearchText(`${entry.label} ${categoryLabel(entry.category ?? 'other')}`).includes(needle);
  const visible = entries.filter(matches);
  const keyless = visible.filter((entry) => entry.availability !== 'requires_credentials');
  const groups: ProviderTileGroup[] = [{ key: 'nokey', entries: keyless }];
  for (const category of order) {
    groups.push({ key: category, entries: visible.filter((entry) => entry.availability === 'requires_credentials' && (entry.category ?? 'other') === category) });
  }
  return groups.filter((group) => group.entries.length > 0);
}
