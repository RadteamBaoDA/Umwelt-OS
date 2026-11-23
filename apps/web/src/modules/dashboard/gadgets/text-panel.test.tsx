import userEvent from '@testing-library/user-event';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';
import { TextPanel } from './text-panel';

const mocks = vi.hoisted(() => ({ list: vi.fn(), openDrawer: vi.fn(), isOwner: true }));
vi.mock('@/modules/knowledge', () => ({ documentKeys: { all: ['documents'] }, listGadgetDocumentProjections: mocks.list }));
vi.mock('../gadget-freshness', () => ({ useReportGadgetFreshness: () => undefined }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ openDrawer: mocks.openDrawer }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isOwner: mocks.isOwner }) }));

/** Renders the text gadget with a fresh query client and the dashboard catalog. */
function renderText() {
  const instance = { id: 't1', title: '', definition: { renderer: 'text', source_ids: ['s1'], scope: {}, revision: 1 } } as never;
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <NextIntlClientProvider locale="en-US" messages={{ dashboard: getCatalog('en-us', 'dashboard') }}>
        <TextPanel instance={instance} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

describe('TextPanel record Ask AI', () => {
  it('sends an anchored one-item selection for the clicked document', async () => {
    mocks.isOwner = true;
    mocks.openDrawer.mockReset();
    mocks.list.mockResolvedValue({
      next_cursor: null,
      items: [
        {
          document_id: 'd5',
          document_version_id: 'v5',
          version_number: 1,
          source_id: 's1',
          title: 'Text record',
          excerpt: 'Body of the text record.',
          published_at: null,
          observed_at: '2026-10-30T12:16:00Z',
        },
      ],
    });
    renderText();
    await userEvent.click(await screen.findByRole('button', { name: 'Ask AI' }));
    expect(mocks.openDrawer).toHaveBeenCalledWith({
      context: { kind: 'selection', anchor: true, items: [{ sourceId: 's1', documentId: 'd5', documentVersionId: 'v5' }] },
    });
  });

  it('hides Ask AI from non-owners', async () => {
    mocks.isOwner = false;
    mocks.list.mockResolvedValue({
      next_cursor: null,
      items: [
        {
          document_id: 'd5',
          document_version_id: 'v5',
          version_number: 1,
          source_id: 's1',
          title: 'Text record',
          excerpt: 'Body.',
          published_at: null,
          observed_at: null,
        },
      ],
    });
    renderText();
    await screen.findByText('Text record');
    expect(screen.queryByRole('button', { name: 'Ask AI' })).toBeNull();
    mocks.isOwner = true;
  });
});
