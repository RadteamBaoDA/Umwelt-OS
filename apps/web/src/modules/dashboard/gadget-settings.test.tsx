import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { gadgetSettingsMessages } from '@/core/messages/gadget-settings';
import { sourcesMessages } from '@/core/messages/sources';
import { uiSweepMessages } from '@/core/messages/ui-sweep';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
vi.mock('next/link', () => ({ default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a> }));
const api = vi.hoisted(() => ({
  listGadgetSources: vi.fn(async () => ({ items: [], next_cursor: null })), listCompatibleSourceCounts: vi.fn(async () => ({})), listGadgetRenderers: vi.fn(async () => []),
  listGadgetTemplates: vi.fn(async () => []), getMetricCatalog: vi.fn(async () => ({})),
}));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

import { GadgetSettings } from './gadget-settings';

const messages = { gadgetSettings: gadgetSettingsMessages['en-us'], dashboard: dashboardMessages['en-us'], sources: sourcesMessages['en-us'], uiSweep: uiSweepMessages['en-us'] };
const instance = {
  id: 'i1', title: null, definition: { id: 'd1', name: 'My feed', revision: 1, renderer: 'news_feed', source_ids: [], scope: {}, filters: { limit: 25 }, highlight_rules: [], config_version: 1, runtime_state: 'available' },
} as never;

afterEach(() => cleanup());

describe('GadgetSettings', () => {
  it('presents the instance title as optional, after the gadget name, with help text', async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <NextIntlClientProvider locale="en-US" messages={messages}>
          <GadgetSettings open onOpenChange={() => {}} instance={instance} dashboardId="dash" dashboardRevision={1} />
        </NextIntlClientProvider>
      </QueryClientProvider>);
    const name = await screen.findByLabelText('Name');
    const title = screen.getByLabelText('Title on this dashboard (optional)');
    expect(name.compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText('Leave empty to use the gadget name')).toBeTruthy();
    expect(title.getAttribute('aria-describedby')).toBeTruthy();
  });
});
