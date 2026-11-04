import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { BuiltinSource } from './api';
import { BuiltinOptions } from './builtin-options';

const base: BuiltinSource = {
  id: 'vn_stock', label: 'Vietnam stocks', category: 'economy', eligibility: 'review', terms_url: 'https://e.org', terms_checked_on: '2026-11-04', attribution: 'x',
  execution: 'supported', source_id: null, enabled: false, data_shapes: ['measurement'], applicable_options: ['name', 'schedule_interval_minutes', 'vn_symbols'],
};
const view = (builtin: BuiltinSource, values = {}) => {
  const onChange = vi.fn();
  render(<NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
    <BuiltinOptions builtin={builtin} values={values} onChange={onChange} showSchedule />
  </NextIntlClientProvider>);
  return onChange;
};
afterEach(() => cleanup());

describe('BuiltinOptions interval floor and tickers', () => {
  it('hides intervals below min_interval_minutes and shows the minimum hint', async () => {
    view({ ...base, min_interval_minutes: 360 });
    expect(screen.getByText('Minimum: every 6 h (source limit)')).toBeTruthy();
    await userEvent.click(screen.getByRole('combobox', { name: 'Collection schedule' }));
    expect(screen.getByRole('option', { name: 'Every 360 minutes' })).toBeTruthy();
    expect(screen.getByRole('option', { name: 'Every 1440 minutes' })).toBeTruthy();
    expect(screen.queryByRole('option', { name: 'Every 15 minutes' })).toBeNull();
    expect(screen.queryByRole('option', { name: 'Every 60 minutes' })).toBeNull();
  });

  it('treats a missing floor as 15 minutes: every value, no hint', async () => {
    view(base);
    expect(screen.queryByText(/Minimum: every/)).toBeNull();
    await userEvent.click(screen.getByRole('combobox', { name: 'Collection schedule' }));
    expect(screen.getByRole('option', { name: 'Every 15 minutes' })).toBeTruthy();
  });

  it('reports upper-cased unique vn_symbols and undefined when emptied', () => {
    const onChange = view(base);
    const input = screen.getByLabelText('Tickers');
    fireEvent.change(input, { target: { value: 'fpt, vcb, fpt' } });
    expect(onChange).toHaveBeenLastCalledWith('vn_symbols', ['FPT', 'VCB']);
    fireEvent.change(input, { target: { value: '' } });
    expect(onChange).toHaveBeenLastCalledWith('vn_symbols', undefined);
  });
});
