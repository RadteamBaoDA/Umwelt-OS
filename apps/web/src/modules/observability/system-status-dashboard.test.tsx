import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { observabilityMessages } from './messages';
import { SystemStatusDashboard } from './system-status-dashboard';

const api = vi.hoisted(() => ({ history: vi.fn(), settings: vi.fn() }));
vi.mock('./api', () => ({
  getSystemHistory: api.history, getHistorySettings: api.settings,
  getSystemHealth: () => new Promise(() => undefined), getQueueSummary: () => new Promise(() => undefined),
  getQuality: () => new Promise(() => undefined), listRuns: () => new Promise(() => undefined),
  getDatabaseStats: () => new Promise(() => undefined),
}));
vi.mock('./history-retention', () => ({ HistoryRetention: () => null }));
vi.mock('./database-panel', () => ({ DatabasePanel: () => null }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
// Radix Select is awkward in jsdom; expose the controlled value on a native select.
vi.mock('@/components/ui/select', () => ({
  Select: ({ value, children }: { value: string; children: React.ReactNode }) => <select aria-label="range" value={value} onChange={() => undefined}>{children}</select>,
  SelectTrigger: () => null, SelectValue: () => null, SelectContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  SelectItem: ({ value, children }: { value: string; children: React.ReactNode }) => <option value={value}>{children}</option>,
}));

/** Renders the dashboard with fresh query state. */
function view() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><NextIntlClientProvider locale="en-US" messages={{ observability: observabilityMessages['en-us'], uiSweep: {} }} onError={() => undefined}><SystemStatusDashboard /></NextIntlClientProvider></QueryClientProvider>);
}

describe('SystemStatusDashboard history controls', () => {
  beforeEach(() => {
    api.history.mockReset(); api.settings.mockReset();
    api.history.mockResolvedValue({ range: '1h', bucket_seconds: 60, retention_days: 7, generated_at: '2026-01-01T00:00:00Z', oldest_sample_at: null, buckets: [] });
  });
  it('defaults the range select to 1 hour and requests 1h', async () => {
    api.settings.mockResolvedValue({ retention_days: 7, sample_interval_minutes: 1, revision: 1 });
    view();
    expect((screen.getByLabelText('range') as HTMLSelectElement).value).toBe('1h');
    await waitFor(() => expect(api.history).toHaveBeenCalledWith('1h'));
  });
  it('explains that collection is off instead of waiting for a first sample', async () => {
    api.settings.mockResolvedValue({ retention_days: 7, sample_interval_minutes: 0, revision: 1 });
    view();
    expect((await screen.findAllByText(/Automatic collection is off/)).length).toBeGreaterThan(0);
  });
});
