import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

const api = vi.hoisted(() => ({ getOnboardingState: vi.fn(), saveOnboardingStep: vi.fn(), apiRequest: vi.fn() }));
vi.mock('./onboarding-api', () => ({ getOnboardingState: api.getOnboardingState, saveOnboardingStep: api.saveOnboardingStep }));
vi.mock('@/core/api', async (original) => ({ ...(await original<typeof import('@/core/api')>()), apiRequest: api.apiRequest }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 't' }) }));
vi.mock('@/modules/sources/api', () => ({ listSources: vi.fn().mockResolvedValue({ items: [] }) }));
vi.mock('@/modules/search/api', () => ({ getSearchIndexStatus: vi.fn().mockResolvedValue({ status: 'unavailable', indexed_items: 0, failed_items: 0, model_id: null }) }));
vi.mock('./onboarding-topics', () => ({ OnboardingTopics: () => null }));
vi.mock('./ai-settings', () => ({ AISettingsWorkspace: () => <div data-testid="models-workspace" /> }));

import { OnboardingPage } from './onboarding-page';

const state = { configuration_revision: 1, current_step: 'ai_privacy', data_choice: null, completed_at: null };

/** Mounts the onboarding page in English with a fresh query client. */
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ onboarding: messages['en-us'].onboarding }}><OnboardingPage /></NextIntlClientProvider></QueryClientProvider>);
}

describe('OnboardingPage AI step', () => {
  beforeEach(() => {
    Object.values(api).forEach((fn) => fn.mockReset());
    api.getOnboardingState.mockResolvedValue(state);
    api.apiRequest.mockResolvedValue({ providers: [], endpoint_policy_denied: false, defaults: {}, capabilities: [], privacy: { allow_remote_reasoning: false, allow_remote_embeddings: false } });
    api.saveOnboardingStep.mockResolvedValue({ ...state, current_step: 'capability' });
  });
  afterEach(cleanup);

  it('embeds the models workspace on demand instead of linking away', async () => {
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Add an AI provider here' }));
    expect(screen.getByTestId('models-workspace')).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Open Models settings' })).toBeNull();
  });

  it('skip path advances to the next step', async () => {
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Skip: use local only' }));
    await waitFor(() => expect(api.saveOnboardingStep).toHaveBeenCalledWith('capability', 1, 't', null));
  });

  it('omits the Upcoming caption in the step rail', async () => {
    mount();
    await screen.findByRole('button', { name: 'Skip: use local only' });
    expect(screen.queryByText('Upcoming')).toBeNull();
    expect(screen.getAllByText('Current step').length).toBeGreaterThan(0);
  });
});
