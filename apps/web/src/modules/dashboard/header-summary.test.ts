import { describe, expect, it } from 'vitest';
import { buildHeaderSummary } from './header-summary';

const t = (key: string, values?: Record<string, string | number>) => `${key}:${values ? Object.values(values).join(',') : ''}`;

describe('buildHeaderSummary', () => {
  it('keeps the default-skin summary to freshness and failing sources', () => {
    expect(buildHeaderSummary(t, '9 min ago', 2)).toBe('summaryUpdated:9 min ago · staleSources.summary:2');
    expect(buildHeaderSummary(t, null, 0)).toBeNull();
  });

  it('adds source and live-event counts for the terminal statusline in order', () => {
    expect(buildHeaderSummary(t, '9 min ago', 0, { sources: 14, liveEvents: 37 })).toBe(
      'summaryUpdated:9 min ago · summarySources:14 · summaryLiveEvents:37',
    );
  });

  it('omits unknown or empty fragments', () => {
    expect(buildHeaderSummary(t, null, 0, { sources: 0, liveEvents: null })).toBeNull();
    expect(buildHeaderSummary(t, null, 0, { sources: 0, liveEvents: 0 })).toBe('summaryLiveEvents:0');
  });

  it('adds the gadget count for non-Overview groups after the other counts', () => {
    expect(buildHeaderSummary(t, '9 min ago', 0, { sources: 0, liveEvents: null, gadgets: 17 })).toBe(
      'summaryUpdated:9 min ago · summaryGadgets:17',
    );
  });
});
