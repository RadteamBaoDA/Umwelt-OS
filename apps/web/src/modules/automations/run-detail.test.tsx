import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import type { Automation, Run } from './api';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ openDrawer: vi.fn() }) }));
const api = vi.hoisted(() => ({ listRuns: vi.fn(), getRuleConversationId: vi.fn(), decideAction: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<object>()), ...api }));

import { RunDetail } from './run-detail';

const rule = { id: 'a1', name: 'Daily brief', revision: 1, actions: [] } as unknown as Automation;

describe('RunDetail no_new_data', () => {
  it('shows "No new data since" with the run finish time instead of the raw code', async () => {
    const run = {
      id: 'r1',
      automation_id: 'a1',
      revision: 1,
      trigger_type: 'schedule',
      depth: 0,
      status: 'succeeded',
      attempts: 1,
      created_at: '2026-11-18T07:00:00Z',
      finished_at: '2026-11-18T07:05:00Z',
      actions: [
        { ordinal: 1, type: 'generate_brief', status: 'succeeded', attempts: 1, error_code: 'no_new_data', result_reference: 'brief:b1' },
      ],
    } as unknown as Run;
    api.listRuns.mockResolvedValue({ items: [run] });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
          <RunDetail rule={rule} csrfToken="c" onBack={vi.fn()} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/No new data since .*7:05/)).toBeInTheDocument();
    expect(screen.queryByText(/Code: no_new_data/)).toBeNull();
  });
});
