import { describe, expect, it } from 'vitest';
import { OPTION_SPECS } from './builtin-option-specs';

/** Documents the server enum lists mirrored from modules/connectors/provider_options.py (reads nothing from the backend). */
describe('builtin option specs', () => {
  const spec = (key: string) => OPTION_SPECS.find((item) => item.key === key)!;
  it('mirrors server enum values and caps', () => {
    expect(spec('hn_list').values).toEqual(['top', 'new', 'best', 'ask', 'show']);
    expect(spec('usgs_min_magnitude').values).toEqual(['significant', '4.5', '2.5', '1.0', 'all']);
    expect(spec('gdacs_alert_levels').values).toEqual(['Green', 'Orange', 'Red']);
    expect(spec('nvd_severity').values).toEqual(['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']);
    expect(spec('crypto_symbols')).toMatchObject({ max: 10 });
    expect(spec('crypto_symbols').values).toHaveLength(12);
    expect(spec('coin_ids')).toMatchObject({ max: 5 });
    expect(spec('coin_ids').values).toHaveLength(10);
    expect(spec('fx_quotes')).toMatchObject({ max: 10 });
    expect(spec('oecd_countries')).toMatchObject({ max: 10 });
    expect(spec('eonet_categories').values).toHaveLength(13);
    expect(spec('nws_area').values).toHaveLength(51);
    expect(spec('nvd_keyword')).toMatchObject({ kind: 'text', maxLen: 100 });
    expect(spec('gdelt_query')).toMatchObject({ kind: 'text', maxLen: 100, native: true });
  });
  it('has unique keys', () => {
    expect(new Set(OPTION_SPECS.map((item) => item.key)).size).toBe(OPTION_SPECS.length);
  });
});
