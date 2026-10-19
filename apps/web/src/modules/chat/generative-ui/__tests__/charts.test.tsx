import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { GenerativeBarChart } from '../components/charts/bar-chart';
import { CHART_PALETTE, resolveSeriesColor } from '../components/charts/config';
import { GenerativePieChart } from '../components/charts/pie-chart';

describe('Generative UI Charts & Config', () => {
  it('exposes the 5 semantic CSS chart tokens and cycles safely via resolveSeriesColor', () => {
    expect(CHART_PALETTE).toEqual([
      'var(--chart-1)',
      'var(--chart-2)',
      'var(--chart-3)',
      'var(--chart-4)',
      'var(--chart-5)',
    ]);
    expect(resolveSeriesColor(0)).toBe('var(--chart-1)');
    expect(resolveSeriesColor(5)).toBe('var(--chart-1)');
    expect(resolveSeriesColor(2, 'var(--chart-4)')).toBe('var(--chart-4)');
    // Rejects literal hex color overrides in favor of semantic tokens
    expect(resolveSeriesColor(1, '#ff0000')).toBe('var(--chart-2)');
  });

  it('renders GenerativeBarChart with accessible summary table and locale-formatted values', () => {
    render(
      <GenerativeBarChart
        title="Ingestion Throughput"
        description="Documents indexed per source"
        currency="USD"
        locale="en-us"
        data={[
          { label: 'SEC Filings', value: 1250.5 },
          { label: 'Research Notes', value: 840 },
        ]}
      />,
    );

    expect(screen.getByTestId('generative-bar-chart')).toBeInTheDocument();
    expect(screen.getAllByText('Ingestion Throughput').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByTestId('bar-summary-item-0')).toHaveTextContent('$1,250.5');
    // Screen-reader accessible table rendered by ChartAccessibleTable
    expect(screen.getByRole('table')).toBeInTheDocument();
  });

  it('renders GenerativePieChart with semantic --chart-1..5 tokens, percentage shares, and vi-VN formatting', () => {
    render(
      <GenerativePieChart
        title="Phân bổ Tri thức"
        unit="docs"
        locale="vi-vi"
        data={[
          { label: 'Kinh tế', value: 60 },
          { label: 'Công nghệ', value: 40 },
        ]}
      />,
    );

    expect(screen.getByTestId('generative-pie-chart')).toBeInTheDocument();
    expect(screen.getAllByText('Phân bổ Tri thức').length).toBeGreaterThanOrEqual(1);

    const slice0 = screen.getByTestId('pie-slice-item-0');
    expect(slice0).toHaveTextContent('Kinh tế');
    expect(slice0).toHaveTextContent('60 docs');
    expect(slice0).toHaveTextContent('(60%)');

    const colorDot = slice0.querySelector('[data-color]');
    expect(colorDot?.getAttribute('data-color')).toBe('var(--chart-1)');
  });

  it('renders clean empty states when bar or pie chart data is empty', () => {
    render(
      <>
        <GenerativeBarChart title="Empty Bar" data={[]} />
        <GenerativePieChart title="Empty Pie" data={[]} />
      </>,
    );

    expect(screen.getByTestId('bar-chart-empty')).toBeInTheDocument();
    expect(screen.getByTestId('pie-chart-empty')).toBeInTheDocument();
  });

  it('normalizes backend Python ChartPayload series ({ key, label, color_token }), x_key, and subtitle', () => {
    render(
      <GenerativeBarChart
        title="Backend Metric Comparison"
        subtitle="Extracted from workspace knowledge"
        x_key="category"
        series={[
          {
            key: 'score',
            label: 'Relevance Score',
            color_token: 'var(--chart-3)',
          },
        ]}
        data={[
          { category: 'Chunk A', score: 92 },
          { category: 'Chunk B', score: 85 },
        ]}
      />,
    );

    expect(screen.getByText('Extracted from workspace knowledge')).toBeInTheDocument();
    const item0 = screen.getByTestId('bar-summary-item-0');
    expect(item0).toHaveTextContent('Chunk A');
    expect(item0).toHaveTextContent('92');
    const colorDot = item0.querySelector('[data-color]');
    expect(colorDot?.getAttribute('data-color')).toBe('var(--chart-3)');
  });

  it.each(['bar', 'line', 'scatter', 'distribution'] as const)('renders the %s variant from chartType', (chartType) => {
    render(
      <GenerativeBarChart
        title="Variant"
        locale="en-us"
        chartType={chartType}
        data={[
          { label: '1', value: 3 },
          { label: '2', value: 5 },
        ]}
      />,
    );
    expect(screen.getByTestId('bar-chart-canvas')).toHaveAttribute('data-chart-type', chartType);
  });
});

