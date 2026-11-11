import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { AgentPicker } from './chat-agent-picker';
import { AgentPanels } from './chat-session-panels';

/** Renders under English messages. */
const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
    {ui}
  </NextIntlClientProvider>
);

describe('AgentPicker (C9, R7)', () => {
  it('asks for the profile list only when opened, then lists specialists', async () => {
    const onOpen = vi.fn();
    const profiles = [{ id: 'research', enabled: true, capability: 'full' }] as never;
    const { rerender } = render(
      wrap(<AgentPicker value="assistant" disabled={false} onChange={vi.fn()} profiles={undefined} onOpen={onOpen} />),
    );
    expect(onOpen).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('combobox', { name: 'Run as' }));
    expect(onOpen).toHaveBeenCalledTimes(1);
    rerender(wrap(<AgentPicker value="assistant" disabled={false} onChange={vi.fn()} profiles={profiles} onOpen={onOpen} />));
    expect(await screen.findByRole('option', { name: 'Research' })).toBeTruthy();
  });
});

describe('AgentPanels (R7)', () => {
  it('mounts no agent activity in plain chat', () => {
    const { container } = render(
      wrap(
        <AgentPanels
          conversationId="c1"
          csrfToken="x"
          agentMode={false}
          pendingProfileRun={null}
          activeRun={undefined}
          runs={undefined}
          onCancelRun={vi.fn()}
          cancelling={false}
          onOpenRun={vi.fn()}
        />,
      ),
    );
    // Without the gate this would mount ConversationAgentActivity, which needs a QueryClient and throws.
    expect(container.textContent).toBe('');
  });
});
