import { act, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { ConnectionFooter } from './connection-footer';

const view = (api: 'connected' | 'unavailable', realtime: 'connected' | 'reconnecting') => (
  <NextIntlClientProvider locale="en-US" messages={messages['en-us']}><ConnectionFooter apiStatus={api} realtimeStatus={realtime} /></NextIntlClientProvider>
);

describe('connection footer', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('renders no visible indicator while connected but keeps screen-reader status', () => {
    render(view('connected', 'connected'));
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getAllByRole('status').length).toBeGreaterThan(0);
  });

  it('shows the indicator only after degraded state persists 5 s and hides it on recovery', () => {
    const { rerender } = render(view('connected', 'reconnecting'));
    act(() => { vi.advanceTimersByTime(4900); });
    expect(screen.queryByRole('button')).toBeNull();
    act(() => { vi.advanceTimersByTime(200); });
    expect(screen.getByRole('button')).toBeTruthy();
    rerender(view('connected', 'connected'));
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('restarts the delay when the state recovers before 5 s', () => {
    const { rerender } = render(view('connected', 'reconnecting'));
    act(() => { vi.advanceTimersByTime(3000); });
    rerender(view('connected', 'connected'));
    rerender(view('connected', 'reconnecting'));
    act(() => { vi.advanceTimersByTime(3000); });
    expect(screen.queryByRole('button')).toBeNull();
  });
});
