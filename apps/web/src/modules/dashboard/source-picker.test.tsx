import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { githubMessages } from './github-messages';
import { SOURCE_BACKED_RENDERERS, SourcePicker } from './source-picker';

const api = vi.hoisted(() => ({ listGadgetSources: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), listGadgetSources: api.listGadgetSources }));

describe('provider_observation source picker', () => {
  it('is registered as source-backed over observation providers only', async () => {
    const { provider } = SOURCE_BACKED_RENDERERS.provider_observation;
    expect(provider).toContain('alternative_me');
    api.listGadgetSources.mockResolvedValue({ items: [
      { id: 'a', name: 'Fear and Greed', provider: 'alternative_me', status: 'active' },
      { id: 'b', name: 'My GitHub', provider: 'github', status: 'active' },
    ] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'], github: githubMessages['en-us'] }}>
      <SourcePicker id="p" provider={provider} value={null} onChange={() => {}} /></NextIntlClientProvider></QueryClientProvider>);
    await userEvent.click(await screen.findByRole('combobox'));
    expect(await screen.findByText('Fear and Greed')).toBeTruthy();
    expect(screen.queryByText('My GitHub')).toBeNull();
  });
});
