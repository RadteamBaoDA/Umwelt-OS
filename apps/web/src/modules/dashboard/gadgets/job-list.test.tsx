import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { commonMessages } from '@/core/messages/common';
import { dashboardMessages } from '@/core/messages/dashboard';
import { JobListGadget } from './job-list';

const api = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock('@/modules/knowledge/api', () => ({ documentKeys: { all: ['documents'] }, listGadgetDocumentProjections: api.list }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

const job = (id: string, title: string, url: string, company: string, at: string) => ({
  document_id: id, document_version_id: `v${id}`, version_number: 1, source_id: 's1', title, canonical_url: url,
  published_at: at, observed_at: at, excerpt: `Company: ${company}; Location: Hanoi`,
  provider_metadata: { provider: 'vietnamworks_jobs', source_fields: { publisher: 'VietnamWorks' } },
});

describe('JobListGadget', () => {
  it('dedupes by URL, sorts newest first and filters by text', async () => {
    api.list.mockResolvedValue({ next_cursor: null, items: [
      job('1', 'Go Developer', 'https://j/1', 'Acme', '2026-10-01T00:00:00Z'),
      job('2', 'Go Developer', 'https://j/1', 'Acme', '2026-10-01T00:00:00Z'),
      job('3', 'Python Engineer', 'https://j/3', 'Beta', '2026-10-05T00:00:00Z'),
    ] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], common: commonMessages['en-us'] }}>
        <JobListGadget instance={{ id: 'g1', title: '', definition: { source_ids: ['s1'] } } as never} />
      </NextIntlClientProvider></QueryClientProvider>);
    const links = await screen.findAllByRole('link');
    expect(links.map((l) => l.textContent)).toEqual(['Python Engineer', 'Go Developer']);
    expect(links[0].getAttribute('rel')).toContain('noopener');
    fireEvent.change(screen.getByLabelText('Filter jobs'), { target: { value: 'acme' } });
    expect(screen.getAllByRole('link').map((l) => l.textContent)).toEqual(['Go Developer']);
  });

  it('shows 5 postings, then expands in place', async () => {
    api.list.mockResolvedValue({ next_cursor: null, items: Array.from({ length: 8 }, (_, i) => job(`${i}`, `Role ${i}`, `https://j/${i}`, 'Acme', `2026-10-0${i + 1}T00:00:00Z`)) });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], common: commonMessages['en-us'] }}>
        <JobListGadget instance={{ id: 'g1', title: '', definition: { source_ids: ['s1'] } } as never} />
      </NextIntlClientProvider></QueryClientProvider>);
    await screen.findByText('Role 7');
    expect(screen.getAllByRole('link')).toHaveLength(5);
    fireEvent.click(screen.getByRole('button', { name: 'Show 3 more' }));
    expect(screen.getAllByRole('link')).toHaveLength(8);
  });
});
