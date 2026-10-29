import { fireEvent, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { ConnectorConfig } from './api';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

import { ProviderScope } from './provider-scope';

const base: ConnectorConfig = { timeout_seconds: 30, timezone: 'UTC', schedule_interval_minutes: 30, history_mode: 'returned_snapshot' };

describe('Mastodon scope form', () => {
  it('offers instance, account and hashtag inputs (not the Telegram fallback) and reports typed values', () => {
    const onChange = vi.fn();
    render(<NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
      <ProviderScope provider="mastodon" configuration={base} disabled={false} resetEpoch={0} onChange={onChange} />
    </NextIntlClientProvider>);
    expect(screen.queryByLabelText(/telegram/i)).toBeNull();
    fireEvent.change(screen.getByLabelText(/instance url/i), { target: { value: 'https://mastodon.social' } });
    fireEvent.change(screen.getByLabelText(/^Account/), { target: { value: 'Mastodon' } });
    fireEvent.change(screen.getByLabelText(/^Hashtag/), { target: { value: '#news' } });
    expect(onChange).toHaveBeenCalledWith('mastodon_instance_url', 'https://mastodon.social');
    expect(onChange).toHaveBeenCalledWith('mastodon_account', 'Mastodon');
    expect(onChange).toHaveBeenCalledWith('mastodon_hashtag', 'news');
  });
});
