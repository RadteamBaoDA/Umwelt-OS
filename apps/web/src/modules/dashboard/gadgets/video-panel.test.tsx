import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { VideoPanel } from './video-panel';

const item = (id: string, image_url?: string | null) => ({
  document_id: id, document_version_id: id, title: `Video ${id}`, canonical_url: `https://www.youtube.com/watch?v=${id}`,
  image_url, provider_metadata: { provider: 'youtube', source_fields: { author: 'Chan' } },
});
vi.mock('@/modules/knowledge', () => ({
  documentKeys: { all: ['documents'] },
  listGadgetDocumentProjections: vi.fn(async () => ({ items: [item('a', '/api/v1/media/proxy?x=1'), item('b', null), ...['c', 'd', 'e', 'f', 'g'].map((id) => item(id, null))], next_cursor: null })),
}));
vi.mock('../gadget-freshness', () => ({ useReportGadgetFreshness: () => undefined }));

describe('VideoPanel thumbnails', () => {
  it('shows a thumbnail only for items with an image and hides it on error', async () => {
    const instance = { id: 'v', definition: { renderer: 'video_panel', source_ids: ['s'], scope: {} } } as never;
    render(
      <QueryClientProvider client={new QueryClient()}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], daily: { streamCount: '{count}' } }}>
          <VideoPanel instance={instance} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    await screen.findByText('Video b');
    const imgs = document.querySelectorAll('img');
    expect(imgs).toHaveLength(1);
    fireEvent.error(imgs[0]!);
    expect(document.querySelector('img')).toBeNull();
  });

  it('shows 5 videos and expands in place', async () => {
    const instance = { id: 'v', definition: { renderer: 'video_panel', source_ids: ['s'], scope: {} } } as never;
    render(
      <QueryClientProvider client={new QueryClient()}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], daily: { streamCount: '{count}' } }}>
          <VideoPanel instance={instance} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    await screen.findByText('Video b');
    expect(screen.queryByText('Video g')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show 2 more' }));
    expect(screen.getByText('Video g')).toBeTruthy();
  });
});
