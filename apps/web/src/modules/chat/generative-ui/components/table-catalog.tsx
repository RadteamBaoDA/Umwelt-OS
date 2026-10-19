'use client';

import * as React from 'react';
import {
  ArrowDownIcon,
  ArrowUpDownIcon,
  ArrowUpIcon,
  MessageSquarePlusIcon,
  SearchIcon,
  TableIcon,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { formatChartValue } from '@/core/charts/recharts-wrapper';
import { normalizeFormattingLocale, type AppLocaleId } from '@/core/i18n';
import { chatMessages } from '@/core/messages/chat';
import { resolveWidgetLocale } from './plan-card';

/** Supported cell formatting modes in TableCatalog. */
export type TableColumnFormat = 'text' | 'number' | 'currency' | 'percent' | 'badge';

/** Column descriptor for the native structured TableCatalog component. */
export interface TableColumn {
  /** Field key used to look up values on object rows (or stringified index for tuple rows). */
  key: string;
  /** Visible column header label. */
  label: string;
  /** Whether the column supports interactive ascending/descending sorting (defaults to true). */
  sortable?: boolean;
  /** Cell display format ('text' | 'number' | 'currency' | 'percent' | 'badge'). */
  format?: TableColumnFormat;
  /** Alias for format used by GenerativeUIPayload.table schema. */
  type?: TableColumnFormat;
  /** Optional text alignment emitted by backend `TableColumnSpec.align`. */
  align?: 'left' | 'right' | 'center';
  /** ISO currency code when format === 'currency' (defaults to 'USD'). */
  currency?: string;
  /** Optional unit suffix when format === 'number'. */
  unit?: string;
  /** Fractional digit precision for numeric/currency/percent formatting. */
  precision?: number;
}

/** Row input accepted by TableCatalog: either keyed record objects or positional cell tuples. */
export type TableRowData =
  | Record<string, unknown>
  | Array<string | number | boolean | null | undefined>;

/** Active column sort state. */
export interface TableSortState {
  /** Key of the currently sorted column. */
  columnKey: string;
  /** Sort direction. */
  direction: 'asc' | 'desc';
}

/** Props accepted by the native structured TableCatalog widget. */
export interface TableCatalogProps {
  /** Table headline title. */
  title: string;
  /** Optional subtitle or context description. */
  description?: string;
  /** Backend `TablePayload.subtitle` alias. */
  subtitle?: string;
  /** Column descriptors or plain header labels. */
  columns: Array<TableColumn | string>;
  /** Data rows as keyed objects or positional cell arrays. */
  rows?: TableRowData[];
  /** Backend `TablePayload.records` fallback when `rows` is omitted. */
  records?: Array<Record<string, unknown>>;
  /** Optional data provenance or source attribution note. */
  source?: string;
  /** Optional custom label for the per-row follow-up action button. */
  rowActionLabel?: string;
  /** Application locale ('en-us' | 'vi-vi') used for number/currency/percent formatting and UI labels. */
  locale?: AppLocaleId;
  /** Callback invoked when the user triggers a row-level follow-up action. */
  onSendPrompt?: (prompt: string) => void;
}

/** Bilingual UI copy for TableCatalog controls and states. */
const TABLE_COPY: Record<
  AppLocaleId,
  {
    emptyTitle: string;
    emptyFiltered: string;
    sourcePrefix: string;
    actionsHeader: string;
    defaultActionLabel: string;
    rowCountLabel: (visible: number, total: number) => string;
    sortByLabel: (label: string) => string;
    rowPrompt: (title: string, summary: string) => string;
  }
> = {
  'en-us': {
    emptyTitle: 'No rows available in this table.',
    emptyFiltered: 'No matching rows found for the current filter.',
    sourcePrefix: 'Source',
    actionsHeader: 'Actions',
    defaultActionLabel: 'Analyze',
    rowCountLabel: (visible, total) =>
      visible === total ? `${total} rows` : `${visible} of ${total} rows`,
    sortByLabel: (label) => `Sort by ${label}`,
    rowPrompt: (title, summary) => `Analyze row from "${title}": ${summary}`,
  },
  'vi-vi': {
    emptyTitle: 'Không có dữ liệu trong bảng này.',
    emptyFiltered: 'Không tìm thấy dòng phù hợp với bộ lọc hiện tại.',
    sourcePrefix: 'Nguồn',
    actionsHeader: 'Thao tác',
    defaultActionLabel: 'Phân tích',
    rowCountLabel: (visible, total) =>
      visible === total ? `${total} dòng` : `${visible} / ${total} dòng`,
    sortByLabel: (label) => `Sắp xếp theo ${label}`,
    rowPrompt: (title, summary) => `Phân tích dòng từ bảng "${title}": ${summary}`,
  },
};

/**
 * Normalizes mixed string or TableColumn descriptors into canonical TableColumn objects.
 *
 * @param columns - Raw column definitions passed to TableCatalog.
 * @returns Normalized TableColumn array.
 */
function normalizeColumns(columns: Array<TableColumn | string>): TableColumn[] {
  return (columns ?? []).map((col, idx) => {
    if (typeof col === 'string') {
      return {
        key: col || String(idx),
        label: col,
        sortable: true,
        format: 'text',
      };
    }
    return {
      ...col,
      sortable: col.sortable ?? true,
      format: col.format ?? col.type ?? 'text',
    };
  });
}

/**
 * Extracts a raw cell value from either a keyed object row or a positional tuple row.
 *
 * @param row - Row record or array.
 * @param column - Normalized column descriptor.
 * @param columnIndex - Zero-based position of the column.
 * @returns Raw cell value.
 */
function getRawCellValue(row: TableRowData, column: TableColumn, columnIndex: number): unknown {
  if (Array.isArray(row)) {
    return row[columnIndex];
  }
  if (column.key in row) {
    return row[column.key];
  }
  if (column.label in row) {
    return row[column.label];
  }
  return undefined;
}

/**
 * Resolves a semantic shadcn Badge variant based on status/category text.
 *
 * @param value - Text content of the badge cell.
 * @returns Semantic Badge variant.
 */
function resolveBadgeVariant(
  value: string,
): 'default' | 'secondary' | 'destructive' | 'outline' {
  const normalized = value.trim().toLowerCase();
  if (
    ['active', 'completed', 'ready', 'healthy', 'high', 'approved', 'positive', 'hoàn thành', 'sẵn sàng'].includes(
      normalized,
    )
  ) {
    return 'default';
  }
  if (
    ['failed', 'error', 'critical', 'rejected', 'overdue', 'negative', 'lỗi', 'thất bại'].includes(
      normalized,
    )
  ) {
    return 'destructive';
  }
  if (['pending', 'in_progress', 'in progress', 'medium', 'warning', 'chờ xử lý'].includes(normalized)) {
    return 'secondary';
  }
  return 'outline';
}

/**
 * Formats a raw cell value into a string for display and search indexing using formatChartValue
 * for locale-aware number, currency, and percentage formatting.
 *
 * @param raw - Raw cell value.
 * @param column - Column formatting metadata.
 * @param locale - Active application locale ('en-us' | 'vi-vi').
 * @returns Localized string representation of the cell value.
 */
export function formatTableCellValue(
  raw: unknown,
  column: TableColumn,
  locale: AppLocaleId,
): string {
  if (raw === null || raw === undefined || raw === '') {
    return '—';
  }
  const numVal = typeof raw === 'number' ? raw : Number(raw);
  const isNumeric = !Number.isNaN(numVal) && raw !== '' && typeof raw !== 'boolean';

  if (column.format === 'currency' && isNumeric) {
    return formatChartValue(numVal, {
      currency: column.currency ?? 'USD',
      locale,
      precision: column.precision ?? 2,
    });
  }
  if (column.format === 'percent' && isNumeric) {
    return formatChartValue(numVal, {
      unit: '%',
      locale,
      precision: column.precision ?? 2,
    });
  }
  if ((column.format === 'number' || column.currency || column.unit) && isNumeric) {
    return formatChartValue(numVal, {
      currency: column.currency,
      unit: column.unit,
      locale,
      precision: column.precision ?? 2,
    });
  }
  if (typeof raw === 'number') {
    return formatChartValue(raw, {
      locale,
      precision: column.precision ?? 2,
    });
  }
  return String(raw);
}

/**
 * Native structured TableCatalog widget ported from OpenIntelligentUI and adapted to Umwelt-OS
 * shadcn Table, Input, Badge, and Button primitives. Supports ascending/descending column sorting,
 * instant text filtering across rows, locale-aware currency/number/percent formatting, semantic badges,
 * empty states, and row-level follow-up prompt actions.
 *
 * @param props - TableCatalogProps configuration and callbacks.
 * @returns Accessible, sortable, filterable data table card.
 */
export function TableCatalog({
  title,
  description,
  subtitle,
  columns,
  rows,
  records,
  source,
  rowActionLabel,
  locale,
  onSendPrompt,
}: TableCatalogProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const canonicalLocale = normalizeFormattingLocale(resolvedLocale);
  const copy = TABLE_COPY[resolvedLocale];
  const catalog = chatMessages[resolvedLocale];
  const resolvedDescription = description ?? subtitle;
  const resolvedRows = React.useMemo<TableRowData[]>(
    () => (rows && rows.length > 0 ? rows : (records ?? [])),
    [records, rows],
  );

  const normalizedColumns = React.useMemo(() => normalizeColumns(columns), [columns]);
  const [filterText, setFilterText] = React.useState('');
  const [sortState, setSortState] = React.useState<TableSortState | null>(null);

  /**
   * Cycles sort direction for a clicked column header ('asc' -> 'desc').
   */
  const handleSortColumn = React.useCallback((columnKey: string) => {
    setSortState((prev) => {
      if (!prev || prev.columnKey !== columnKey) {
        return { columnKey, direction: 'asc' };
      }
      return {
        columnKey,
        direction: prev.direction === 'asc' ? 'desc' : 'asc',
      };
    });
  }, []);

  const filteredAndSortedRows = React.useMemo(() => {
    const safeRows = resolvedRows;
    const query = filterText.trim().toLowerCase();

    const filtered = query
      ? safeRows.filter((row) =>
          normalizedColumns.some((col, colIdx) => {
            const raw = getRawCellValue(row, col, colIdx);
            const formatted = formatTableCellValue(raw, col, resolvedLocale);
            return (
              String(raw ?? '')
                .toLowerCase()
                .includes(query) || formatted.toLowerCase().includes(query)
            );
          }),
        )
      : [...safeRows];

    if (!sortState) {
      return filtered;
    }

    const colIdx = normalizedColumns.findIndex((c) => c.key === sortState.columnKey);
    if (colIdx === -1) {
      return filtered;
    }
    const targetColumn = normalizedColumns[colIdx];
    const multiplier = sortState.direction === 'asc' ? 1 : -1;

    return [...filtered].sort((rowA, rowB) => {
      const valA = getRawCellValue(rowA, targetColumn, colIdx);
      const valB = getRawCellValue(rowB, targetColumn, colIdx);

      const numA = typeof valA === 'number' ? valA : Number(valA);
      const numB = typeof valB === 'number' ? valB : Number(valB);

      if (
        !Number.isNaN(numA) &&
        !Number.isNaN(numB) &&
        valA !== '' &&
        valB !== '' &&
        valA !== null &&
        valB !== null &&
        typeof valA !== 'boolean' &&
        typeof valB !== 'boolean'
      ) {
        return (numA - numB) * multiplier;
      }

      return (
        String(valA ?? '').localeCompare(String(valB ?? ''), canonicalLocale, {
          numeric: true,
          sensitivity: 'base',
        }) * multiplier
      );
    });
  }, [canonicalLocale, filterText, normalizedColumns, resolvedLocale, resolvedRows, sortState]);

  /**
   * Builds and sends a follow-up prompt summarizing the selected row.
   */
  const handleRowAction = React.useCallback(
    (row: TableRowData) => {
      if (!onSendPrompt) return;
      if (!Array.isArray(row) && typeof row.actionPrompt === 'string' && row.actionPrompt.trim()) {
        onSendPrompt(row.actionPrompt.trim());
        return;
      }
      const summary = normalizedColumns
        .map((col, idx) => `${col.label}: ${formatTableCellValue(getRawCellValue(row, col, idx), col, resolvedLocale)}`)
        .join(', ');
      onSendPrompt(copy.rowPrompt(title, summary));
    },
    [copy, normalizedColumns, onSendPrompt, resolvedLocale, title],
  );

  return (
    <Card
      data-testid="generative-table-catalog"
      className="my-2 w-full max-w-3xl border-border bg-surface text-foreground shadow-sm"
    >
      <CardHeader className="gap-3 border-b border-border pb-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <TableIcon className="size-4" aria-hidden="true" />
            </span>
            <div>
              <CardTitle className="text-base font-semibold text-foreground">{title}</CardTitle>
              {resolvedDescription && (
                <CardDescription className="mt-0.5 text-xs text-muted-foreground">
                  {resolvedDescription}
                </CardDescription>
              )}
            </div>
          </div>
          <Badge variant="outline" className="font-mono text-xs">
            {copy.rowCountLabel(filteredAndSortedRows.length, resolvedRows.length)}
          </Badge>
        </div>

        <div className="relative flex items-center">
          <SearchIcon
            className="pointer-events-none absolute left-3 size-4 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            type="search"
            value={filterText}
            onChange={(e) => setFilterText(e.target.value)}
            placeholder={catalog.genUiTableFilterPlaceholder}
            aria-label={catalog.genUiTableFilterPlaceholder}
            data-testid="table-catalog-filter-input"
            className="min-h-9 pl-9 text-xs"
          />
        </div>
      </CardHeader>

      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow className="bg-muted/30">
              {normalizedColumns.map((col) => {
                const isSorted = sortState?.columnKey === col.key;
                const ariaSort = isSorted
                  ? sortState.direction === 'asc'
                    ? 'ascending'
                    : 'descending'
                  : 'none';

                return (
                  <TableHead key={col.key} aria-sort={ariaSort} className="px-3 py-2 text-xs">
                    {col.sortable !== false ? (
                      <button
                        type="button"
                        onClick={() => handleSortColumn(col.key)}
                        aria-label={copy.sortByLabel(col.label)}
                        data-testid={`table-sort-${col.key}`}
                        className="inline-flex items-center gap-1.5 font-semibold text-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <span>{col.label}</span>
                        {isSorted ? (
                          sortState.direction === 'asc' ? (
                            <ArrowUpIcon className="size-3.5 text-primary" aria-hidden="true" />
                          ) : (
                            <ArrowDownIcon className="size-3.5 text-primary" aria-hidden="true" />
                          )
                        ) : (
                          <ArrowUpDownIcon
                            className="size-3.5 text-muted-foreground/70"
                            aria-hidden="true"
                          />
                        )}
                      </button>
                    ) : (
                      <span className="font-semibold text-foreground">{col.label}</span>
                    )}
                  </TableHead>
                );
              })}
              {onSendPrompt && (
                <TableHead className="px-3 py-2 text-right text-xs font-semibold text-foreground">
                  {copy.actionsHeader}
                </TableHead>
              )}
            </TableRow>
          </TableHeader>

          <TableBody>
            {filteredAndSortedRows.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={normalizedColumns.length + (onSendPrompt ? 1 : 0)}
                  data-testid="table-catalog-empty"
                  className="py-8 text-center text-xs text-muted-foreground"
                >
                  {resolvedRows.length === 0 ? copy.emptyTitle : copy.emptyFiltered}
                </TableCell>
              </TableRow>
            ) : (
              filteredAndSortedRows.map((row, rowIdx) => {
                const rowId =
                  !Array.isArray(row) && (typeof row.id === 'string' || typeof row.id === 'number')
                    ? String(row.id)
                    : String(rowIdx);

                return (
                  <TableRow key={rowId} data-testid={`table-row-${rowIdx}`}>
                    {normalizedColumns.map((col, colIdx) => {
                      const raw = getRawCellValue(row, col, colIdx);
                      const formatted = formatTableCellValue(raw, col, resolvedLocale);

                      return (
                        <TableCell key={col.key} className="px-3 py-2.5 text-xs text-foreground">
                          {col.format === 'badge' && raw !== null && raw !== undefined && raw !== '' ? (
                            <Badge variant={resolveBadgeVariant(String(raw))}>
                              {String(raw)}
                            </Badge>
                          ) : (
                            <span
                              className={
                                col.format === 'currency' ||
                                col.format === 'number' ||
                                col.format === 'percent'
                                  ? 'font-mono'
                                  : undefined
                              }
                            >
                              {formatted}
                            </span>
                          )}
                        </TableCell>
                      );
                    })}

                    {onSendPrompt && (
                      <TableCell className="px-3 py-2 text-right">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => handleRowAction(row)}
                          data-testid={`table-row-action-${rowIdx}`}
                          className="min-h-8 px-2.5 py-1 text-xs"
                        >
                          <MessageSquarePlusIcon className="size-3.5" aria-hidden="true" />
                          <span>{rowActionLabel ?? copy.defaultActionLabel}</span>
                        </Button>
                      </TableCell>
                    )}
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </CardContent>

      {source && (
        <CardFooter className="border-t border-border py-2.5 text-[11px] text-muted-foreground">
          <span>
            <strong className="font-medium text-foreground">{copy.sourcePrefix}:</strong> {source}
          </span>
        </CardFooter>
      )}
    </Card>
  );
}

/** Alias preserving upstream DataTable naming compatibility. */
export const DataTable = TableCatalog;
