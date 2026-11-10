import { act, render, renderHook, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import React from 'react';
import { describe, expect, it } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { ShowMoreButton, useShowMore } from './show-more';

const nums = Array.from({ length: 8 }, (_, i) => i);

describe('useShowMore', () => {
  it('shows the first 5, expands, collapses and resets on key change', () => {
    const { result, rerender } = renderHook(({ key }) => useShowMore(nums, 5, key), { initialProps: { key: 'a' } });
    expect(result.current.visible).toHaveLength(5);
    expect(result.current.hiddenCount).toBe(3);
    act(() => result.current.toggle());
    expect(result.current.visible).toHaveLength(8);
    expect(result.current.expanded).toBe(true);
    expect(result.current.hiddenCount).toBe(0);
    rerender({ key: 'b' });
    expect(result.current.expanded).toBe(false);
    expect(result.current.visible).toHaveLength(5);
  });
});

describe('ShowMoreButton', () => {
  function Harness({ items, locale = 'en-us' }: { items: number[]; locale?: 'en-us' | 'vi-vi' }) {
    const state = useShowMore(items, 5);
    return (
      <NextIntlClientProvider locale={locale === 'en-us' ? 'en-US' : 'vi-VN'} messages={{ dashboard: dashboardMessages[locale] }}>
        <ShowMoreButton state={state} />
      </NextIntlClientProvider>
    );
  }

  it('toggles label and aria-expanded', async () => {
    render(<Harness items={nums} />);
    const button = screen.getByRole('button', { name: 'Show 3 more' });
    expect(button.getAttribute('aria-expanded')).toBe('false');
    await userEvent.click(button);
    expect(screen.getByRole('button', { name: 'Show less' }).getAttribute('aria-expanded')).toBe('true');
  });

  it('renders Vietnamese copy and nothing for short lists', () => {
    const { unmount } = render(<Harness items={nums} locale="vi-vi" />);
    expect(screen.getByRole('button', { name: 'Xem thêm 3' })).toBeTruthy();
    unmount();
    render(<Harness items={[1, 2]} />);
    expect(screen.queryByRole('button')).toBeNull();
  });
});
