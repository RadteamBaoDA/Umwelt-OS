import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { BuiltinSource } from './api';
import { BuiltinOptions } from './builtin-options';

const rss: BuiltinSource = {
  id: 'tuoitre_news', label: 'Tuổi Trẻ', category: 'news', eligibility: 'open', terms_url: 'https://e.org', terms_checked_on: '2026-10-07', attribution: 'x',
  execution: 'supported', source_id: null, enabled: false, data_shapes: ['news'],
  applicable_options: ['name', 'schedule_interval_minutes', 'max_items', 'include_keywords', 'exclude_keywords', 'max_age_days', 'feed_sections'],
  sections: [{ slug: 'home', label_en: 'Home', label_vi: 'Trang chủ' }, { slug: 'world', label_en: 'World', label_vi: 'Thế giới' }], default_section: 'home',
};
const snapshot: BuiltinSource = { ...rss, id: 'usgs', applicable_options: ['name', 'schedule_interval_minutes'], sections: null, default_section: null };

const view = (builtin: BuiltinSource, values = {}, extra: Record<string, unknown> = {}) => {
  const onChange = vi.fn();
  render(<NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
    <BuiltinOptions builtin={builtin} values={values} onChange={onChange} {...extra} />
  </NextIntlClientProvider>);
  return onChange;
};
afterEach(() => cleanup());

describe('BuiltinOptions', () => {
  it('renders sections, filters and the cost estimate only for applicable options', () => {
    view(rss, { feed_sections: ['home', 'world'], schedule_interval_minutes: 60 }, { showName: true, showSchedule: true });
    expect(screen.getByLabelText('Source name')).toBeTruthy();
    expect(screen.getByRole('checkbox', { name: 'Home' })).toBeTruthy();
    expect(screen.getByLabelText('Max new items per sync')).toBeTruthy();
    expect(screen.getByText(/2 sections × every 60 min ≈ 48 requests\/day/)).toBeTruthy();
    expect(screen.getByText(/Applies to new fetches/)).toBeTruthy();
  });

  it('hides options the built-in does not accept', () => {
    view(snapshot, {}, { showName: true, showSchedule: true });
    expect(screen.getByLabelText('Source name')).toBeTruthy();
    expect(screen.queryByRole('checkbox', { name: 'Home' })).toBeNull();
    expect(screen.queryByLabelText('Max new items per sync')).toBeNull();
    expect(screen.queryByLabelText(/Only items containing/)).toBeNull();
    expect(screen.queryByLabelText(/Ignore items older/)).toBeNull();
  });

  it('hides name and schedule unless asked (Configure owns them)', () => {
    view(rss);
    expect(screen.queryByLabelText('Source name')).toBeNull();
    expect(screen.queryByLabelText('Collection schedule')).toBeNull();
  });

  it('reports section toggles', () => {
    const onChange = view(rss, { feed_sections: ['home'] });
    fireEvent.click(screen.getByRole('checkbox', { name: 'World' }));
    expect(onChange).toHaveBeenCalledWith('feed_sections', ['home', 'world']);
  });

  it('keeps at least one section selected', () => {
    const onChange = view(rss, { feed_sections: ['home'] });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Home' }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it('parses comma separated keywords and numeric limits', () => {
    const onChange = view(rss, { max_age_days: 7 });
    fireEvent.change(screen.getByLabelText(/Only items containing/), { target: { value: 'bitcoin, vàng ,' } });
    expect(onChange).toHaveBeenCalledWith('include_keywords', ['bitcoin', 'vàng']);
    fireEvent.change(screen.getByLabelText('Max new items per sync'), { target: { value: '25' } });
    expect(onChange).toHaveBeenCalledWith('max_items', 25);
    fireEvent.change(screen.getByLabelText(/Ignore items older/), { target: { value: '' } });
    expect(onChange).toHaveBeenCalledWith('max_age_days', undefined);
  });

  it('labels sections in Vietnamese', () => {
    render(<NextIntlClientProvider locale="vi-VN" messages={{ sources: sourcesMessages['vi-vi'] }}><BuiltinOptions builtin={rss} values={{ feed_sections: ['home'] }} onChange={vi.fn()} /></NextIntlClientProvider>);
    expect(screen.getByRole('checkbox', { name: 'Thế giới' })).toBeTruthy();
  });

  describe('per-type options', () => {
    const typed = (id: string, options: string[]): BuiltinSource => ({ ...snapshot, id, applicable_options: ['name', ...options] });
    const NATIVE_HINT = /restarts collection from the recent window; older matching items are not backfilled/;

    it('shows only the options the type accepts (usgs)', () => {
      view(typed('usgs', ['usgs_min_magnitude', 'usgs_period', 'region']));
      expect(screen.getByText('Minimum magnitude')).toBeTruthy();
      expect(screen.getByText('Period')).toBeTruthy();
      expect(screen.getByText('Region')).toBeTruthy();
      expect(screen.queryByText('Trading pairs')).toBeNull();
      expect(screen.queryByLabelText(/Only items containing/)).toBeNull();
      expect(screen.queryByText(NATIVE_HINT)).toBeNull();
    });

    it('shows trading pairs for binance and no keyword filters', () => {
      view(typed('binance', ['crypto_symbols']));
      expect(screen.getByRole('checkbox', { name: 'ETHUSDT' })).toBeTruthy();
      expect(screen.queryByLabelText(/Only items containing/)).toBeNull();
    });

    it('reports an enum choice', async () => {
      const onChange = view(typed('hn_top', ['hn_list']));
      await userEvent.click(screen.getByRole('combobox', { name: 'Story list' }));
      await userEvent.click(await screen.findByRole('option', { name: 'Best' }));
      expect(onChange).toHaveBeenCalledWith('hn_list', 'best');
    });

    it('reports multi-select toggles and an emptied selection', () => {
      const onChange = view(typed('binance', ['crypto_symbols']), { crypto_symbols: ['BTCUSDT'] });
      fireEvent.click(screen.getByRole('checkbox', { name: 'ETHUSDT' }));
      expect(onChange).toHaveBeenCalledWith('crypto_symbols', ['BTCUSDT', 'ETHUSDT']);
      fireEvent.click(screen.getByRole('checkbox', { name: 'BTCUSDT' }));
      expect(onChange).toHaveBeenLastCalledWith('crypto_symbols', []);
    });

    it('enforces the selection cap (coin_ids at 5)', () => {
      const five = ['btc-bitcoin', 'eth-ethereum', 'bnb-binance-coin', 'sol-solana', 'xrp-xrp'];
      const onChange = view(typed('coinpaprika', ['coin_ids']), { coin_ids: five });
      const extra = screen.getByRole('checkbox', { name: 'ada-cardano' }) as HTMLButtonElement;
      expect(extra.disabled).toBe(true);
      fireEvent.click(extra);
      expect(onChange).not.toHaveBeenCalled();
    });

    it('reports bounded numbers, text and the switch', () => {
      const onChange = view(typed('nvd', ['nvd_window_days', 'nvd_keyword']), {});
      fireEvent.change(screen.getByLabelText('Look-back window (days)'), { target: { value: '3' } });
      expect(onChange).toHaveBeenCalledWith('nvd_window_days', 3);
      fireEvent.change(screen.getByLabelText('Keyword'), { target: { value: 'openssl' } });
      expect(onChange).toHaveBeenCalledWith('nvd_keyword', 'openssl');
      expect((screen.getByLabelText('Keyword') as HTMLInputElement).maxLength).toBe(100);
      cleanup();
      const filled = view(typed('nvd', ['nvd_keyword']), { nvd_keyword: 'openssl' });
      fireEvent.change(screen.getByLabelText('Keyword'), { target: { value: '' } });
      expect(filled).toHaveBeenLastCalledWith('nvd_keyword', undefined);
      cleanup();
      const kev = view(typed('cisa_kev', ['kev_ransomware_only']));
      fireEvent.click(screen.getByRole('switch', { name: 'Known ransomware campaigns only' }));
      expect(kev).toHaveBeenCalledWith('kev_ransomware_only', true);
    });

    it('explains native-parameter options restart from the recent window', () => {
      view(typed('gdelt_economy', ['gdelt_query']));
      expect(screen.getByText(NATIVE_HINT)).toBeTruthy();
    });

    it('labels per-type options in Vietnamese', () => {
      render(<NextIntlClientProvider locale="vi-VN" messages={{ sources: sourcesMessages['vi-vi'] }}><BuiltinOptions builtin={typed('usgs', ['region'])} values={{}} onChange={vi.fn()} /></NextIntlClientProvider>);
      expect(screen.getByText('Khu vực')).toBeTruthy();
    });
  });
});
