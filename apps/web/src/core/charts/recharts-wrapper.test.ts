import { describe, expect, it } from 'vitest';
import { formatYTick, paddedYDomain, yTicks } from './recharts-wrapper';

describe('formatYTick', () => {
  it('keeps 5 ticks over a narrow range distinct', () => {
    const span = 77484 - 77373;
    const labels = [0, 1, 2, 3, 4].map((i) => formatYTick(77373 + (span * i) / 4, 'en-us', span));
    expect(new Set(labels).size).toBe(5);
  });

  it('uses one fixed number of decimals across ticks', () => {
    const span = 77484 - 77373;
    const labels = [0, 1, 2, 3, 4].map((i) => formatYTick(77373 + (span * i) / 4, 'en-us', span));
    expect(new Set(labels.map((l) => l.replace(/^[^.]*\.?/, '').length)).size).toBe(1);
  });

  it('keeps compact notation for large spans', () => {
    expect(formatYTick(1_200_000, 'en-us', 1_000_000)).toBe('1.2M');
  });
});

describe('yTicks', () => {
  it('yields distinct formatted labels on a narrow snapped domain', () => {
    const domain = paddedYDomain([{ x: 1, value: 77373 }, { x: 2, value: 77484 }] as never, [{ dataKey: 'value' }] as never);
    const ticks = yTicks(domain)!;
    const span = (domain[1] as number) - (domain[0] as number);
    const labels = ticks.map((v) => formatYTick(v, 'en-us', span));
    expect(ticks.length).toBeGreaterThan(2);
    expect(new Set(labels).size).toBe(labels.length);
  });
});
