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
import { SampleDataBadge } from './sample-data-badge';
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
import { TablePagination, usePagination } from '@/components/ui/pagination';
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
import { genUiCopy, resolveWidgetLocale, TABLE_COPY } from '../copy';
import { BareButton } from '@/components/bare-button';

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
  /** True when rows are illustrative sample data (renders a "Sample data" badge). */
  isSample?: boolean;
  /** Optional custom label for the per-row follow-up action button. */
  rowActionLabel?: string;
  /** Application locale ('en-us' | 'vi-vi') used for number/currency/percent formatting and UI labels. */
  locale?: AppLocaleId;
  /** Callback invoked when the user triggers a row-level follow-up action. */
  onSendPrompt?: (prompt: string) => void;
}

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
  const precision = column.precision ?? 2;

  if (isNumeric) {
    const options = numericFormatOptions(column, locale, precision);
    if (options) return formatChartValue(numVal, options);
  }
  if (typeof raw === 'number') {
    return formatChartValue(raw, { locale, precision });
  }
  return String(raw);
}

/**
 * Chooses the numeric formatting for a column: currency (default USD), percent, or plain number with optional
 * currency/unit; undefined when the column declares none of them (the value then falls through to default formatting).
 */
function numericFormatOptions(column: TableColumn, locale: AppLocaleId, precision: number): Parameters<typeof formatChartValue>[1] | undefined {
  if (column.format === 'currency') return { currency: column.currency ?? 'USD', locale, precision };
  if (column.format === 'percent') return { unit: '%', locale, precision };
  if (column.format === 'number' || column.currency || column.unit) {
    return { currency: column.currency, unit: column.unit, locale, precision };
  }
  return undefined;
}

/** Whether both raw values compare numerically (neither blank, null or boolean, and both parse as numbers). */
function bothNumeric(valA: unknown, valB: unknown, numA: number, numB: number): boolean {
  return (
    !Number.isNaN(numA) && !Number.isNaN(numB)
    && valA !== '' && valB !== '' && valA !== null && valB !== null
    && typeof valA !== 'boolean' && typeof valB !== 'boolean'
  );
}

/**
 * Filters rows by a case-insensitive query over raw and formatted cell text, then sorts by the chosen column
 * (numeric when both values are numeric, otherwise natural locale compare).
 *
 * @param rows - All rows.
 * @param columns - Normalized columns.
 * @param filterText - Raw filter input.
 * @param sortState - Active sort, or null for source order.
 * @param locale - Widget locale used to format cells for matching.
 * @param canonicalLocale - Formatting locale used for string comparison.
 * @returns A new array (never the input array instance).
 */
function filterAndSortRows(
  rows: TableRowData[],
  columns: TableColumn[],
  filterText: string,
  sortState: TableSortState | null,
  locale: AppLocaleId,
  canonicalLocale: string,
): TableRowData[] {
  const query = filterText.trim().toLowerCase();

  const filtered = query
    ? rows.filter((row) =>
        columns.some((col, colIdx) => {
          const raw = getRawCellValue(row, col, colIdx);
          const formatted = formatTableCellValue(raw, col, locale);
          return (
            String(raw ?? '')
              .toLowerCase()
              .includes(query) || formatted.toLowerCase().includes(query)
          );
        }),
      )
    : [...rows];

  if (!sortState) {
    return filtered;
  }

  const colIdx = columns.findIndex((c) => c.key === sortState.columnKey);
  if (colIdx === -1) {
    return filtered;
  }
  const targetColumn = columns[colIdx];
  const multiplier = sortState.direction === 'asc' ? 1 : -1;

  return [...filtered].sort((rowA, rowB) => {
    const valA = getRawCellValue(rowA, targetColumn, colIdx);
    const valB = getRawCellValue(rowB, targetColumn, colIdx);

    const numA = typeof valA === 'number' ? valA : Number(valA);
    const numB = typeof valB === 'number' ? valB : Number(valB);

    if (bothNumeric(valA, valB, numA, numB)) {
      return (numA - numB) * multiplier;
    }

    return (
      String(valA ?? '').localeCompare(String(valB ?? ''), canonicalLocale, {
        numeric: true,
        sensitivity: 'base',
      }) * multiplier
    );
  });
}

/** Summarizes a row as `label: value` pairs for the row-action follow-up prompt. */
function summarizeRow(row: TableRowData, columns: TableColumn[], locale: AppLocaleId): string {
  return columns
    .map((col, idx) => `${col.label}: ${formatTableCellValue(getRawCellValue(row, col, idx), col, locale)}`)
    .join(', ');
}

type TableCopy = (typeof TABLE_COPY)[keyof typeof TABLE_COPY];

/** Sort glyph of a column header: direction arrow when sorted, neutral up/down otherwise. */
function SortIcon({ direction }: { direction: TableSortState['direction'] | null }) {
  if (direction === 'asc') return <ArrowUpIcon className="size-3.5 text-primary" aria-hidden="true" />;
  if (direction === 'desc') return <ArrowDownIcon className="size-3.5 text-primary" aria-hidden="true" />;
  return <ArrowUpDownIcon className="size-3.5 text-muted-foreground/70" aria-hidden="true" />;
}

/** Header row with sortable column buttons (aria-sort) and the optional actions column. */
function CatalogHeaderRow({ columns, sortState, copy, hasActions, onSort }: {
  columns: TableColumn[];
  sortState: TableSortState | null;
  copy: TableCopy;
  hasActions: boolean;
  onSort: (columnKey: string) => void;
}) {
  return (
    <TableRow className="bg-muted/30">
      {columns.map((col) => {
        const isSorted = sortState?.columnKey === col.key;
        const direction = isSorted ? sortState.direction : null;
        const ariaSort = direction === 'asc' ? 'ascending' : direction === 'desc' ? 'descending' : 'none';

        return (
          <TableHead key={col.key} aria-sort={ariaSort} className="px-3 py-2 text-xs">
            {col.sortable !== false ? (
              <BareButton
                type="button"
                onClick={() => onSort(col.key)}
                aria-label={copy.sortByLabel(col.label)}
                data-testid={`table-sort-${col.key}`}
                className="inline-flex items-center gap-1.5 font-semibold text-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span>{col.label}</span>
                <SortIcon direction={direction} />
              </BareButton>
            ) : (
              <span className="font-semibold text-foreground">{col.label}</span>
            )}
          </TableHead>
        );
      })}
      {hasActions && (
        <TableHead className="px-3 py-2 text-right text-xs font-semibold text-foreground">
          {copy.actionsHeader}
        </TableHead>
      )}
    </TableRow>
  );
}

/** One body cell: a semantic badge for non-empty badge columns, otherwise formatted text (tabular for numbers). */
function CatalogCell({ raw, column, locale }: { raw: unknown; column: TableColumn; locale: AppLocaleId }) {
  const formatted = formatTableCellValue(raw, column, locale);
  const isBadge = column.format === 'badge' && raw !== null && raw !== undefined && raw !== '';
  const tabular = column.format === 'currency' || column.format === 'number' || column.format === 'percent';
  return (
    <TableCell className="px-3 py-2.5 text-xs text-foreground">
      {isBadge ? (
        <Badge variant={resolveBadgeVariant(String(raw))}>
          {String(raw)}
        </Badge>
      ) : (
        <span className={tabular ? 'tabular-nums' : undefined}>
          {formatted}
        </span>
      )}
    </TableCell>
  );
}

/** One data row with its cells and the optional row action button. */
function CatalogRow({ row, rowIdx, columns, locale, actionLabel, onAction }: {
  row: TableRowData;
  rowIdx: number;
  columns: TableColumn[];
  locale: AppLocaleId;
  /** Present only when the widget can send prompts. */
  actionLabel?: string;
  onAction?: (row: TableRowData) => void;
}) {
  return (
    <TableRow data-testid={`table-row-${rowIdx}`}>
      {columns.map((col, colIdx) => (
        <CatalogCell key={col.key} raw={getRawCellValue(row, col, colIdx)} column={col} locale={locale} />
      ))}

      {onAction && (
        <TableCell className="px-3 py-2 text-right">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onAction(row)}
            data-testid={`table-row-action-${rowIdx}`}
            className="min-h-8 px-2.5 py-1 text-xs"
          >
            <MessageSquarePlusIcon className="size-3.5" aria-hidden="true" />
            <span>{actionLabel}</span>
          </Button>
        </TableCell>
      )}
    </TableRow>
  );
}

/** Single full-width row shown when no rows are visible (no data at all, or the filter matched nothing). */
function CatalogEmptyRow({ colSpan, text }: { colSpan: number; text: string }) {
  return (
    <TableRow>
      <TableCell
        colSpan={colSpan}
        data-testid="table-catalog-empty"
        className="py-8 text-center text-xs text-muted-foreground"
      >
        {text}
      </TableCell>
    </TableRow>
  );
}

/** Stable React key of a row: its string/number `id`, else the global index. */
function catalogRowKey(row: TableRowData, rowIdx: number): string {
  return !Array.isArray(row) && (typeof row.id === 'string' || typeof row.id === 'number') ? String(row.id) : String(rowIdx);
}

/** Card header: title block, sample/row-count badges and the filter box. */
function CatalogHeader({ title, description, isSample, locale, copy, visibleCount, totalCount, filterText, filterPlaceholder, onFilter }: {
  title: string;
  description?: string;
  isSample?: boolean;
  locale: AppLocaleId;
  copy: TableCopy;
  visibleCount: number;
  totalCount: number;
  filterText: string;
  filterPlaceholder: string;
  onFilter: (value: string) => void;
}) {
  return (
    <CardHeader className="gap-3 border-b border-border pb-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <TableIcon className="size-4" aria-hidden="true" />
          </span>
          <div>
            <CardTitle className="text-base font-semibold text-foreground">{title}</CardTitle>
            {description && (
              <CardDescription className="mt-0.5 text-xs text-muted-foreground">
                {description}
              </CardDescription>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {isSample && <SampleDataBadge locale={locale} />}
          <Badge variant="outline" className="tabular-nums text-xs">
          {copy.rowCountLabel(visibleCount, totalCount)}
          </Badge>
        </div>
      </div>

      <div className="relative flex items-center">
        <SearchIcon
          className="pointer-events-none absolute left-3 size-4 text-muted-foreground"
          aria-hidden="true"
        />
        <Input
          type="search"
          value={filterText}
          onChange={(e) => onFilter(e.target.value)}
          placeholder={filterPlaceholder}
          aria-label={filterPlaceholder}
          data-testid="table-catalog-filter-input"
          className="min-h-9 pl-9 text-xs"
        />
      </div>
    </CardHeader>
  );
}

/**
 * Filter text, sort state, the derived visible rows and their pager.
 *
 * @returns Handlers reset nothing themselves: callers move the pager back to page 1 when the filter or sort changes.
 */
function useCatalogView(rows: TableRowData[], columns: TableColumn[], locale: AppLocaleId, canonicalLocale: string) {
  const [filterText, setFilterText] = React.useState('');
  const [sortState, setSortState] = React.useState<TableSortState | null>(null);

  /** Cycles sort direction for a clicked column header ('asc' -> 'desc'). */
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

  const filteredAndSortedRows = React.useMemo(
    () => filterAndSortRows(rows, columns, filterText, sortState, locale, canonicalLocale),
    [canonicalLocale, filterText, columns, locale, rows, sortState],
  );
  const pager = usePagination(filteredAndSortedRows);
  return { filterText, setFilterText, sortState, handleSortColumn, filteredAndSortedRows, pager };
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
  isSample,
  rowActionLabel,
  locale,
  onSendPrompt,
}: TableCatalogProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const canonicalLocale = normalizeFormattingLocale(resolvedLocale);
  const copy = TABLE_COPY[resolvedLocale];
  const catalog = genUiCopy(resolvedLocale);
  const resolvedRows = React.useMemo<TableRowData[]>(
    () => (rows && rows.length > 0 ? rows : (records ?? [])),
    [records, rows],
  );

  const normalizedColumns = React.useMemo(() => normalizeColumns(columns), [columns]);
  const view = useCatalogView(resolvedRows, normalizedColumns, resolvedLocale, canonicalLocale);
  const { filterText, setFilterText, sortState, handleSortColumn, filteredAndSortedRows, pager } = view;

  /** Builds and sends a follow-up prompt summarizing the selected row (a row-provided `actionPrompt` wins). */
  const handleRowAction = React.useCallback(
    (row: TableRowData) => {
      if (!onSendPrompt) return;
      if (!Array.isArray(row) && typeof row.actionPrompt === 'string' && row.actionPrompt.trim()) {
        onSendPrompt(row.actionPrompt.trim());
        return;
      }
      onSendPrompt(copy.rowPrompt(title, summarizeRow(row, normalizedColumns, resolvedLocale)));
    },
    [copy, normalizedColumns, onSendPrompt, resolvedLocale, title],
  );

  return (
    <Card
      data-testid="generative-table-catalog"
      className="my-2 w-full max-w-3xl border-border bg-surface text-foreground"
    >
      <CatalogHeader
        title={title}
        description={description ?? subtitle}
        isSample={isSample}
        locale={resolvedLocale}
        copy={copy}
        visibleCount={filteredAndSortedRows.length}
        totalCount={resolvedRows.length}
        filterText={filterText}
        filterPlaceholder={catalog.genUiTableFilterPlaceholder}
        onFilter={(value) => { setFilterText(value); pager.setPage(1); }}
      />

      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <CatalogHeaderRow
              columns={normalizedColumns}
              sortState={sortState}
              copy={copy}
              hasActions={Boolean(onSendPrompt)}
              onSort={(columnKey) => { handleSortColumn(columnKey); pager.setPage(1); }}
            />
          </TableHeader>

          <TableBody>
            {filteredAndSortedRows.length === 0 ? (
              <CatalogEmptyRow colSpan={normalizedColumns.length + (onSendPrompt ? 1 : 0)} text={resolvedRows.length === 0 ? copy.emptyTitle : copy.emptyFiltered} />
            ) : (
              pager.pageItems.map((row, pageIdx) => {
                // Global index keeps ids and test ids stable across pages.
                const rowIdx = (pager.page - 1) * pager.pageSize + pageIdx;
                return (
                  <CatalogRow
                    key={catalogRowKey(row, rowIdx)}
                    row={row}
                    rowIdx={rowIdx}
                    columns={normalizedColumns}
                    locale={resolvedLocale}
                    actionLabel={rowActionLabel ?? copy.defaultActionLabel}
                    onAction={onSendPrompt ? handleRowAction : undefined}
                  />
                );
              })
            )}
          </TableBody>
        </Table>
        <div className="px-3 pb-2"><TablePagination {...pager} /></div>
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
