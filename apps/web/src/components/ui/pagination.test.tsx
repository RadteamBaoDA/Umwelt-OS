import { act, cleanup, render, renderHook, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it } from 'vitest';
import { messages } from '@/core/messages';
import { TablePagination, usePagination } from './pagination';

afterEach(() => cleanup());

const rows = (n: number) => Array.from({ length: n }, (_, i) => i);

describe('usePagination', () => {
  it('clamps the page when items shrink', () => {
    const { result, rerender } = renderHook(({ items }) => usePagination(items, 10), { initialProps: { items: rows(35) } });
    act(() => result.current.setPage(4));
    expect(result.current.page).toBe(4);
    rerender({ items: rows(12) });
    expect(result.current.page).toBe(2);
    expect(result.current.pageItems).toEqual([10, 11]);
  });

  it('resets to page 1 when the page size changes', () => {
    const { result } = renderHook(() => usePagination(rows(100), 10));
    act(() => result.current.setPage(3));
    act(() => result.current.setPageSize(50));
    expect(result.current.page).toBe(1);
    expect(result.current.pageItems).toHaveLength(50);
  });
});

/** Renders the pager over `n` rows inside the English intl provider. */
function Harness({ n }: { n: number }) {
  const p = usePagination(rows(n), 10);
  return <TablePagination {...p} />;
}
const mount = (n: number) => render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><Harness n={n} /></NextIntlClientProvider>);

describe('TablePagination', () => {
  it('is hidden when total fits the smallest page size', () => {
    const { container } = mount(10);
    expect(container.textContent).toBe('');
  });

  it('shows the range text', () => {
    mount(25);
    expect(screen.getByText('1–10 of 25')).toBeTruthy();
  });
});
