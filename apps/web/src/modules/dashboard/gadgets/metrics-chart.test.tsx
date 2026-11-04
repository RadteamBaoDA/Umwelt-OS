import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { MetricsChart } from './metrics-chart';

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
