import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
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

  it('pie honors x_key and series[0].key from backend payloads', () => {
    render(
      <GenerativePieChart
        title="Spend"
        categoryKey="category"
        series={[{ key: 'amount' }]}
        data={[
          { category: 'Alpha', amount: 60 },
          { category: 'Beta', amount: 40 },
        ]}
      />,
    );
    expect(screen.getByTestId('pie-slice-item-0')).toHaveTextContent('Alpha');
    expect(screen.getByTestId('pie-slice-item-0')).toHaveTextContent('60');
    expect(screen.getByTestId('pie-slice-item-1')).toHaveTextContent('Beta');
    expect(screen.getByTestId('pie-slice-item-1')).toHaveTextContent('40');
    expect(screen.queryByText('#1')).not.toBeInTheDocument();
  });

  it('bar and pie show the sample badge and source only when flagged', () => {
    const { rerender } = render(
      <>
        <GenerativeBarChart title="B" data={[{ label: 'a', value: 1 }]} isSample source="Demo set" locale="en-us" />
        <GenerativePieChart title="P" data={[{ label: 'a', value: 1 }]} isSample source="Demo set" locale="en-us" />
      </>,
    );
    expect(screen.getAllByTestId('sample-data-badge')).toHaveLength(2);
    expect(screen.getByTestId('bar-chart-source')).toHaveTextContent('Demo set');
    expect(screen.getByTestId('pie-chart-source')).toHaveTextContent('Demo set');
    rerender(
      <>
        <GenerativeBarChart title="B" data={[{ label: 'a', value: 1 }]} isSample={false} locale="en-us" />
        <GenerativePieChart title="P" data={[{ label: 'a', value: 1 }]} isSample={false} locale="en-us" />
      </>,
    );
    expect(screen.queryByTestId('sample-data-badge')).not.toBeInTheDocument();
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


describe('Chart export', () => {
  const evil = '<img src=x onerror=alert(1)>';
  const data = [
    { label: evil, value: 30 },
    { label: 'B', value: 70 },
  ];

  /** Captures the HTML passed to clipboard.writeText after clicking copy. */
  async function copied(): Promise<string> {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    fireEvent.click(screen.getAllByTestId('generative-ui-copy-btn')[0]);
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    return writeText.mock.calls[0][0] as string;
  }

  it.each(['bar', 'line', 'scatter', 'distribution'] as const)('exports %s chart as standalone html', async (chartType) => {
    render(<GenerativeBarChart title="Sales & Co" chartType={chartType} data={data} locale="en-us" source="Ledger" />);
    const html = await copied();
    expect(html).toContain('<title>Sales &amp; Co</title>');
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
    expect(html).not.toContain(evil);
    expect(html).toContain('<svg');
    expect(html).toContain('Ledger');
    expect(html).not.toMatch(/https?:\/\/|<script|<img/i);
  });

  it('exports pie chart and downloads an .html file', async () => {
    render(<GenerativePieChart title="Mix" data={data} locale="vi-vi" />);
    const create = vi.fn(() => 'blob:x');
    URL.createObjectURL = create;
    URL.revokeObjectURL = vi.fn();
    fireEvent.click(screen.getAllByTestId('generative-ui-export-btn')[0]);
    expect(create).toHaveBeenCalledTimes(1);
    const blob = (create.mock.calls[0] as unknown as [Blob])[0];
    expect(blob.type).toContain('text/html');
    const html = await copied();
    expect(html).toContain('<path');
    expect(html).toContain('lang="vi"');
    expect(html).not.toMatch(/https?:\/\//i);
  });
});
