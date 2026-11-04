import { describe, expect, it } from 'vitest';
import { formatProviderName } from './metric-label';

describe('formatProviderName', () => {
  it('uses brand spelling for Open-Meteo and humanizes unknown ids', () => {
    expect(formatProviderName('open_meteo')).toBe('Open-Meteo');
    expect(formatProviderName('some_provider')).toBe('Some provider');
  });
});
