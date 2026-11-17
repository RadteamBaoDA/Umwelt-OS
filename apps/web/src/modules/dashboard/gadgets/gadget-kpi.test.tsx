import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DeltaPill, GadgetKpi } from './gadget-kpi';
import { metricBetterDirection } from './metric-direction';
import { Sparkline } from './sparkline';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

const points = [
  { timestamp: '2026-10-01T00:00:00Z', value: 1 },
  { timestamp: '2026-10-02T00:00:00Z', value: 2 },
];

describe('DeltaPill', () => {
  it('is neutral grey with no arrow for zero and null', () => {
    for (const percent of [0, null]) {
      const { container, unmount } = render(<DeltaPill percent={percent} better="up" />);
      expect(screen.getByTestId('delta-pill').dataset.tone).toBe('neutral');
      expect(container.querySelector('svg')).toBeNull();
      unmount();
    }
  });

  it('colours by outcome when lower is better', () => {
    const { unmount } = render(<DeltaPill percent={5} better="down" />);
    expect(screen.getByTestId('delta-pill').dataset.tone).toBe('negative');
    unmount();
    render(<DeltaPill percent={-5} better="down" />);
    expect(screen.getByTestId('delta-pill').dataset.tone).toBe('positive');
  });

  it('stays neutral by default', () => {
    render(<DeltaPill percent={5} />);
    expect(screen.getByTestId('delta-pill').dataset.tone).toBe('neutral');
  });
});

describe('GadgetKpi / Sparkline / direction', () => {
  it('renders the figure with the large scale class', () => {
    render(<GadgetKpi label="AQI" value={42} deltaPercent={0} />);
    expect(screen.getByText('42').className).toContain('text-figure');
  });

  it('renders the sparkline aria-hidden', () => {
    render(<Sparkline data={points} />);
    expect(screen.getByTestId('sparkline').getAttribute('aria-hidden')).toBe('true');
  });

  it('maps lower-is-better keys to down, others neutral', () => {
    expect(metricBetterDirection('air_quality_aqi')).toBe('down');
    expect(metricBetterDirection('btc_price')).toBe('neutral');
    expect(metricBetterDirection(null)).toBe('neutral');
  });
});
