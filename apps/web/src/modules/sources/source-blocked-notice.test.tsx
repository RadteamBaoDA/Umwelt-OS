import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { errorCodesMessages } from '@/core/messages/error-codes';
import { sourcesMessages } from '@/core/messages/sources';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
import { SourceBlockedNotice } from './source-entry';

const show = (source: Parameters<typeof SourceBlockedNotice>[0]['source']) =>
  render(
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'], errorCodes: errorCodesMessages['en-us'] }}>
      <SourceBlockedNotice source={source} />
    </NextIntlClientProvider>,
  );

describe('SourceBlockedNotice', () => {
  it('explains a known blocked code and the failure count', () => {
    show({ blocked_error_code: 'schema_changed', failure_count: 3 });
    expect(screen.getByText('Stopped: the provider changed its data format · 3 failed attempts')).toBeTruthy();
  });
  it('falls back to the humanized code and renders nothing for a healthy source', () => {
    const view = show({ blocked_error_code: 'weird_thing', failure_count: 1 });
    expect(screen.getByText('Stopped: Weird thing · 1 failed attempt')).toBeTruthy();
    view.unmount();
    expect(show({ blocked_error_code: null, failure_count: 0 }).container.textContent).toBe('');
  });
});
