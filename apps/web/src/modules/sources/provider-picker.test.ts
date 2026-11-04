import { describe, expect, it } from 'vitest';
import type { ConnectorCatalogEntry } from './api';
import { buildProviderGroups, foldSearchText } from './provider-picker';

const e = (provider_id: string, label: string, availability: string, category?: string) => ({ provider_id, label, availability, category }) as unknown as ConnectorCatalogEntry;
const order = ['news', 'economy', 'other'];
const entries = [e('a', 'Keyed News', 'requires_credentials', 'news'), e('b', 'Free News', 'available', 'news'), e('c', 'CoinGecko', 'requires_credentials', 'economy'), e('d', 'Tin tức Việt', 'implemented')];
const names = (query: string) => buildProviderGroups(entries, order, (c) => c, query).map((g) => [g.key, g.entries.map((x) => x.provider_id)]);

describe('buildProviderGroups', () => {
  it('puts no-key providers first and credential-requiring ones last', () => {
    expect(names('')).toEqual([['nokey', ['b', 'd']], ['news', ['a']], ['economy', ['c']]]);
  });
  it('filters by label or category, ignoring case and diacritics', () => {
    expect(names('tin tuc viet')).toEqual([['nokey', ['d']]]);
    expect(names('ECONOMY')).toEqual([['economy', ['c']]]);
  });
  it('returns no groups when nothing matches', () => {
    expect(names('zzz')).toEqual([]);
  });
  it('folds Vietnamese d-stroke', () => {
    expect(foldSearchText('Đà Nẵng')).toBe('da nang');
  });
});
