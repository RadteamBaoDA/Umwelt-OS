import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { TablePanel } from './table-panel';

const api = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock('@/modules/knowledge/api', () => ({ documentKeys: { all: ['documents'] }, listGadgetDocumentProjections: api.list }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ openDrawer: vi.fn() }) }));

describe('TablePanel', () => {
  it('renders the security provider title "{cve_id} {severity}" as a title plus severity badge', async () => {
    api.list.mockResolvedValue({ next_cursor: null, items: [{
      document_id: 'd1', document_version_id: 'v1', version_number: 1, source_id: 's1',
      title: 'CVE-2026-5461 MEDIUM', excerpt: 'A vulnerable plugin.', published_at: '2026-10-30T12:16:00Z', observed_at: null,
    }] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}>
      <TablePanel instance={{ id: 'g1', title: '', definition: { source_ids: ['s1'] } } as never} />
    </NextIntlClientProvider></QueryClientProvider>);
    expect(await screen.findByText('Medium')).toBeTruthy();
    expect(screen.getByText('CVE-2026-5461', { exact: false }).textContent).not.toContain('MEDIUM');
  });

  it('hides an excerpt that only repeats the title', async () => {
    api.list.mockResolvedValue({ next_cursor: null, items: [{
      document_id: 'd2', document_version_id: 'v2', version_number: 1, source_id: 's1',
      title: 'Markets rally - Reuters', excerpt: 'Markets  rally Reuters', published_at: '2026-10-30T12:16:00Z', observed_at: null,
    }] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}>
      <TablePanel instance={{ id: 'g1', title: '', definition: { source_ids: ['s1'] } } as never} />
    </NextIntlClientProvider></QueryClientProvider>);
    expect(await screen.findByText('Markets rally - Reuters')).toBeTruthy();
    expect(container.querySelector('p.line-clamp-2')).toBeNull();
  });
});
