import { fireEvent, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { ConnectorConfig } from './api';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

import { ProviderScope } from './provider-scope';

const base: ConnectorConfig = { timeout_seconds: 30, timezone: 'UTC', schedule_interval_minutes: 30, history_mode: 'returned_snapshot', news_site: 'any', news_locale: 'en-US' };

const view = (locale: 'en-us' | 'vi-vi', configuration: ConnectorConfig, onChange = vi.fn()) => {
  render(
    <NextIntlClientProvider locale={locale === 'en-us' ? 'en-US' : 'vi-VN'} messages={{ sources: sourcesMessages[locale] }}>
      <ProviderScope provider="google_news" configuration={configuration} disabled={false} resetEpoch={0} onChange={onChange} />
    </NextIntlClientProvider>);
  return onChange;
};

describe('Google News RSS scope form', () => {
  it('has a required query input, a site select and a language/region select; no URL field', () => {
    view('en-us', { ...base, news_query: '' });
    const query = screen.getByLabelText('Search query') as HTMLInputElement;
    expect(query.required).toBe(true);
    expect(query.maxLength).toBe(200);
    expect(screen.getByLabelText('Publisher site')).toBeTruthy();
    expect(screen.getByLabelText('Language and region')).toBeTruthy();
    expect(screen.queryByLabelText(/url/i)).toBeNull();
    expect(screen.getByText(/no official API contract/)).toBeTruthy();
  });

  it('reports the typed query and selected allowlisted site', async () => {
    const onChange = view('en-us', { ...base, news_query: '' });
    fireEvent.change(screen.getByLabelText('Search query'), { target: { value: 'oil price' } });
    expect(onChange).toHaveBeenCalledWith('news_query', 'oil price');
    fireEvent.click(screen.getByLabelText('Publisher site'));
    fireEvent.click(await screen.findByRole('option', { name: 'reuters.com' }));
    expect(onChange).toHaveBeenCalledWith('news_site', 'reuters.com');
  });

  it('localizes the labels in Vietnamese', () => {
    view('vi-vi', { ...base, news_query: 'giá vàng', news_locale: 'vi-VN' });
    expect(screen.getByLabelText('Từ khóa tìm kiếm')).toBeTruthy();
    expect(screen.getByLabelText('Ngôn ngữ và khu vực')).toBeTruthy();
  });
});
