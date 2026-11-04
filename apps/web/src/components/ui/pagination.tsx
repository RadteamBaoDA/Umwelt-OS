'use client';

import { ChevronLeftIcon, ChevronRightIcon } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Button } from './button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './select';

/** Selectable rows-per-page sizes; the smallest also bounds when the pager is hidden. */
const PAGE_SIZES = [10, 20, 50, 100];

/** Result of {@link usePagination}: the current slice plus the controls `TablePagination` needs. */
export type PaginationState<T> = {
  pageItems: T[];
  page: number;
  pageCount: number;
  pageSize: number;
  total: number;
  setPage: (page: number) => void;
  setPageSize: (size: number) => void;
};

/**
 * Client-side paging over an in-memory list. The page is clamped during render (not in an effect) so a
 * shrinking list (filter, delete) never shows an empty out-of-range page. `setPageSize` returns to page 1.
 * Pure: no network or storage.
 * @param items Full list to page.
 * @param initialPageSize Starting rows per page.
 */
export function usePagination<T>(items: T[], initialPageSize = 20): PaginationState<T> {
  const [rawPage, setRawPage] = useState(1);
  const [pageSize, setSize] = useState(initialPageSize);
  const total = items.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const page = Math.min(Math.max(1, rawPage), pageCount);
  const pageItems = items.slice((page - 1) * pageSize, page * pageSize);
  const setPage = (next: number) => setRawPage(Math.min(Math.max(1, next), pageCount));
  const setPageSize = (size: number) => { setSize(size); setRawPage(1); };
  return { pageItems, page, pageCount, pageSize, total, setPage, setPageSize };
}

type CursorProps = { page: number; hasPrev: boolean; hasNext: boolean; onPrev: () => void; onNext: () => void; disabled?: boolean };
type TablePaginationProps = PaginationState<unknown> | CursorProps;

/**
 * Pager footer for tables. Client mode takes a `usePagination` result (rows-per-page select, range text,
 * prev/next; hidden when everything fits in the smallest page size). Cursor mode (`hasPrev`/`hasNext`)
 * serves server-cursor lists with only a page number and prev/next.
 */
export function TablePagination(props: TablePaginationProps) {
  // Hidden check happens before the translation hook (in PagerBody) so small tables need no intl provider.
  if (!('hasPrev' in props) && props.total <= PAGE_SIZES[0]) return null;
  return <PagerBody {...props} />;
}

/** Renders the visible pager controls; split from `TablePagination` so the hide rule runs before any hook. */
function PagerBody(props: TablePaginationProps) {
  const t = useTranslations('common');
  const cursor = 'hasPrev' in props;
  const prevDisabled = cursor ? !props.hasPrev || props.disabled : props.page <= 1;
  const nextDisabled = cursor ? !props.hasNext || props.disabled : props.page >= props.pageCount;
  const onPrev = () => (cursor ? props.onPrev() : props.setPage(props.page - 1));
  const onNext = () => (cursor ? props.onNext() : props.setPage(props.page + 1));
  return (
    <div className="mt-2 flex flex-wrap items-center justify-end gap-x-4 gap-y-2 text-sm text-muted-foreground">
      {!cursor && (
        <label className="flex items-center gap-2">
          <span>{t('rowsPerPage')}</span>
          <Select value={String(props.pageSize)} onValueChange={(v) => props.setPageSize(Number(v))}>
            <SelectTrigger aria-label={t('rowsPerPage')} className="w-20"><SelectValue /></SelectTrigger>
            <SelectContent>{PAGE_SIZES.map((n) => <SelectItem key={n} value={String(n)}>{n}</SelectItem>)}</SelectContent>
          </Select>
        </label>
      )}
      <span aria-live="polite">
        {cursor
          ? t('pageNumber', { page: props.page })
          : t('pageRange', { start: (props.page - 1) * props.pageSize + 1, end: Math.min(props.page * props.pageSize, props.total), total: props.total })}
      </span>
      <div className="flex items-center gap-1">
        <Button type="button" variant="outline" size="icon" aria-label={t('previousPage')} disabled={prevDisabled} onClick={onPrev}><ChevronLeftIcon size={16} aria-hidden /></Button>
        <Button type="button" variant="outline" size="icon" aria-label={t('nextPage')} disabled={nextDisabled} onClick={onNext}><ChevronRightIcon size={16} aria-hidden /></Button>
      </div>
    </div>
  );
}
