import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { TableCatalog, type TableColumn } from '../components/table-catalog';

const SAMPLE_COLUMNS: TableColumn[] = [
  { key: 'source', label: 'Source', format: 'text' },
  { key: 'cost', label: 'Monthly Cost', format: 'currency', currency: 'USD' },
  { key: 'uptime', label: 'Uptime', format: 'percent' },
  { key: 'status', label: 'Status', format: 'badge' },
];

const SAMPLE_ROWS = [
  { id: 'r1', source: 'GitHub Collector', cost: 120, uptime: 99.8, status: 'Active' },
  { id: 'r2', source: 'ArXiv Feed', cost: 0, uptime: 95.4, status: 'Pending' },
  { id: 'r3', source: 'Market Wire', cost: 450, uptime: 98.2, status: 'Failed' },
];

describe('TableCatalog', () => {
  it('shows the sample badge and source only when is_sample is true', () => {
    const { rerender } = render(
      <TableCatalog title="T" columns={SAMPLE_COLUMNS} rows={SAMPLE_ROWS} source="Demo set" isSample locale="en-us" />,
    );
    expect(screen.getByTestId('sample-data-badge')).toHaveTextContent('Sample data');
    expect(screen.getByText('Demo set')).toBeInTheDocument();
    rerender(<TableCatalog title="T" columns={SAMPLE_COLUMNS} rows={SAMPLE_ROWS} isSample={false} locale="en-us" />);
    expect(screen.queryByTestId('sample-data-badge')).not.toBeInTheDocument();
  });

  it('renders columns, formatted currency/percent cells, and status badges', () => {
    render(
      <TableCatalog
        title="Data Connector Matrix"
        description="Active ingestion pipelines"
        columns={SAMPLE_COLUMNS}
        rows={SAMPLE_ROWS}
        source="Internal Telemetry"
        locale="en-us"
      />,
    );

    expect(screen.getByText('Data Connector Matrix')).toBeInTheDocument();
    expect(screen.getByText('GitHub Collector')).toBeInTheDocument();
    expect(screen.getByText('$120')).toBeInTheDocument();
    expect(screen.getByText('99.8%')).toBeInTheDocument();
    expect(screen.getByText('Active')).toHaveAttribute('data-slot', 'badge');
    expect(screen.getByText(/Internal Telemetry/)).toBeInTheDocument();
  });

  it('sorts rows ascending and descending when clicking a sortable column header', async () => {
    const user = userEvent.setup();
    render(
      <TableCatalog
        title="Data Connector Matrix"
        columns={SAMPLE_COLUMNS}
        rows={SAMPLE_ROWS}
      />,
    );

    const sortByCostBtn = screen.getByTestId('table-sort-cost');

    // First click -> ascending ($0, $120, $450)
    await user.click(sortByCostBtn);
    const firstRowAsc = screen.getByTestId('table-row-0');
    expect(within(firstRowAsc).getByText('ArXiv Feed')).toBeInTheDocument();

    // Second click -> descending ($450, $120, $0)
    await user.click(sortByCostBtn);
    const firstRowDesc = screen.getByTestId('table-row-0');
    expect(within(firstRowDesc).getByText('Market Wire')).toBeInTheDocument();
  });

  it('filters rows by search query and shows empty state when no rows match', async () => {
    const user = userEvent.setup();
    render(
      <TableCatalog
        title="Data Connector Matrix"
        columns={SAMPLE_COLUMNS}
        rows={SAMPLE_ROWS}
      />,
    );

    const filterInput = screen.getByTestId('table-catalog-filter-input');
    await user.type(filterInput, 'ArXiv');

    expect(screen.getByText('ArXiv Feed')).toBeInTheDocument();
    expect(screen.queryByText('GitHub Collector')).not.toBeInTheDocument();

    await user.clear(filterInput);
    await user.type(filterInput, 'NonExistentSourceXYZ');
    expect(screen.getByTestId('table-catalog-empty')).toBeInTheDocument();
  });

  it('formats numbers according to vi-vi (vi-VN) locale and invokes onSendPrompt on row action', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();

    render(
      <TableCatalog
        title="Bảng Nguồn Dữ Liệu"
        columns={SAMPLE_COLUMNS}
        rows={SAMPLE_ROWS}
        locale="vi-vi"
        onSendPrompt={onSendPrompt}
      />,
    );

    // In vi-VN, 99.8% uses a comma decimal separator (99,8%)
    expect(screen.getByText('99,8%')).toBeInTheDocument();

    const firstRowAction = screen.getByTestId('table-row-action-0');
    await user.click(firstRowAction);
    expect(onSendPrompt).toHaveBeenCalledTimes(1);
    expect(onSendPrompt.mock.calls[0][0]).toContain('GitHub Collector');
  });
});

