import * as React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConversationAgentActivity } from './conversation-agent-activity';

const getConversationApprovals = vi.fn();
vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => key }));
vi.mock('./api', () => ({
  getConversationApprovals: (id: string) => getConversationApprovals(id),
  decideApproval: vi.fn(),
}));

function renderActivity(watch: boolean) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ConversationAgentActivity conversationId="c1" csrfToken="t" watch={watch} />
    </QueryClientProvider>,
  );
}

describe('ConversationAgentActivity polling', () => {
  afterEach(() => {
    vi.useRealTimers();
    getConversationApprovals.mockReset();
  });

  it('fetches once and does not poll an idle conversation with no approvals', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    getConversationApprovals.mockResolvedValue([]);
    renderActivity(false);
    await vi.advanceTimersByTimeAsync(100);
    await vi.advanceTimersByTimeAsync(35_000);
    expect(getConversationApprovals).toHaveBeenCalledTimes(1);
  });

  it('polls while an agent run is active', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    getConversationApprovals.mockResolvedValue([]);
    renderActivity(true);
    await vi.advanceTimersByTimeAsync(100);
    const before = getConversationApprovals.mock.calls.length;
    await vi.advanceTimersByTimeAsync(11_000);
    expect(getConversationApprovals.mock.calls.length).toBeGreaterThan(before);
  });
});