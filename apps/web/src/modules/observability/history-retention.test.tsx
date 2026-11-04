import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { observabilityMessages } from './messages';
import { HistoryRetention } from './history-retention';

const api = vi.hoisted(() => ({ get: vi.fn(), save: vi.fn() }));
vi.mock('./api', () => ({ getHistorySettings: api.get, saveHistorySettings: api.save }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
// Radix Select is awkward in jsdom; a native select keeps the test on the save/409 logic.
vi.mock('@/components/ui/select', () => ({
  Select: ({ value, onValueChange, children }: { value: string; onValueChange: (v: string) => void; children: React.ReactNode }) => <select aria-label="history" value={value} onChange={(e) => onValueChange(e.target.value)}>{children}</select>,
  SelectTrigger: () => null, SelectValue: () => null, SelectContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  SelectItem: ({ value, children }: { value: string; children: React.ReactNode }) => <option value={value}>{children}</option>,
}));

/** Renders the retention row with fresh query state. */
function view() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><NextIntlClientProvider locale="en-US" messages={{ observability: observabilityMessages['en-us'] }} onError={() => undefined}><HistoryRetention oldestSampleAt={null} /></NextIntlClientProvider></QueryClientProvider>);
}

describe('HistoryRetention', () => {
  beforeEach(() => { api.get.mockReset(); api.save.mockReset(); });
  it('saves with the loaded revision and shows the saved stamp', async () => {
    api.get.mockResolvedValue({ retention_days: 30, sample_interval_minutes: 1, revision: 4 });
    api.save.mockResolvedValue({ retention_days: 7, sample_interval_minutes: 1, revision: 5 });
    view();
    await waitFor(() => expect((screen.getAllByLabelText('history')[0] as HTMLSelectElement).value).toBe('30'));
    fireEvent.change(screen.getAllByLabelText('history')[0], { target: { value: '7' } });
    await screen.findByText('Saved');
    expect(api.save).toHaveBeenCalledWith('csrf', 4, { retention_days: 7 });
  });
  it('saves the collection interval alone with the loaded revision', async () => {
    api.get.mockResolvedValue({ retention_days: 30, sample_interval_minutes: 1, revision: 4 });
    api.save.mockResolvedValue({ retention_days: 30, sample_interval_minutes: 5, revision: 5 });
    view();
    const interval = (await screen.findAllByLabelText('history'))[1];
    await waitFor(() => expect((interval as HTMLSelectElement).value).toBe('1'));
    fireEvent.change(interval, { target: { value: '5' } });
    await screen.findByText('Saved');
    expect(api.save).toHaveBeenCalledWith('csrf', 4, { sample_interval_minutes: 5 });
  });
  it('refetches and explains on a stale revision (409)', async () => {
    api.get.mockResolvedValueOnce({ retention_days: 30, sample_interval_minutes: 1, revision: 4 }).mockResolvedValue({ retention_days: 90, sample_interval_minutes: 1, revision: 6 });
    api.save.mockRejectedValue(new ApiError(409, 'stale'));
    view();
    await waitFor(() => expect((screen.getAllByLabelText('history')[0] as HTMLSelectElement).value).toBe('30'));
    fireEvent.change(screen.getAllByLabelText('history')[0], { target: { value: '7' } });
    expect((await screen.findByRole('alert')).textContent).toContain('changed elsewhere');
    await waitFor(() => expect((screen.getAllByLabelText('history')[0] as HTMLSelectElement).value).toBe('90'));
  });
});
