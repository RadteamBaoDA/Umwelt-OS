import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { MetricsChart, buildMultiChart } from './metrics-chart';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/charts/recharts-wrapper', async (original) => ({
  ...(await original<typeof import('@/core/charts/recharts-wrapper')>()),
  BaseAreaChart: () => <div data-testid="area" />,
  BaseLineChart: () => <div data-testid="line" />,
  BaseBarChart: () => <div data-testid="bar" />,
}));

const instance = { id: 'c1', title: '', definition: { scope: { symbols: ['BTC'] } } } as never;

/** Renders the chart with the given points inside the message providers it needs. */
function view(values: number[]) {
  const points = values.map((value, i) => ({ timestamp: `2026-10-0${i + 1}T00:00:00Z`, value }));
  return render(<NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], daily: {}, uiSweep: {} }} onError={() => undefined}><MetricsChart instance={instance} initialData={points} /></NextIntlClientProvider>);
}

describe('MetricsChart', () => {
  it('shows a neutral delta with no arrow when the series is flat', () => {
    const { container } = view([5, 5, 5]);
    const pill = screen.getByTestId('delta-pill');
    expect(pill.dataset.tone).toBe('neutral');
    expect(container.querySelector('[data-testid="delta-pill"] svg')).toBeNull();
  });

  it('renders the 40px figure and moves the range tabs under the plot', () => {
    view([5, 6]);
    expect(screen.getByText('6').className).toContain('text-figure');
    const plot = screen.getByTestId('area');
    const tab = screen.getByRole('button', { name: '7D' });
    expect(plot.compareDocumentPosition(tab) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('omits the plot and shows the unknown dash for a single observation', () => {
    view([5]);
    expect(screen.queryByTestId('area')).toBeNull();
    expect(screen.getByTestId('delta-pill').textContent).toBe('—');
    expect(screen.getByRole('button', { name: '7D' })).toBeTruthy();
  });
});

describe('buildMultiChart', () => {
  const pts = (...v: number[]) => v.map((value, i) => ({ timestamp: `2026-10-0${i + 1}T00:00:00Z`, value }));
  it('keeps raw values when units match and merges rows by timestamp', () => {
    const out = buildMultiChart([{ key: 'a', name: 'A', points: pts(10, 20), unit: 'VND' }, { key: 'b', name: 'B', points: pts(1, 2, 3), unit: 'VND' }], 'all');
    expect(out.rebased).toBe(false);
    expect(out.data).toHaveLength(3);
    expect(out.data[2].a).toBeUndefined();
    expect(out.data[1].a).toBe(20);
  });
  it('rebases to percent change when units differ', () => {
    const out = buildMultiChart([{ key: 'a', name: 'A', points: pts(10, 20), currency: 'VND' }, { key: 'b', name: 'B', points: pts(100, 150), currency: 'USD' }], 'all');
    expect(out.rebased).toBe(true);
    expect(out.data[1].a).toBe(100);
    expect(out.data[1].b).toBe(50);
    expect(out.series.every((item) => item.unit === '%')).toBe(true);
  });
});
