import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { RouteLoader } from './route-loader';

const route = vi.hoisted(() => ({ path: '/dashboard' }));
vi.mock('next/navigation', () => ({ usePathname: () => route.path }));

/** Renders the loader with a never-settling query so react-query reports an in-flight fetch. */
function renderLoader() {
  const client = new QueryClient();
  void client.prefetchQuery({ queryKey: ['gadget'], queryFn: () => new Promise(() => {}) });
  return render(
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en" messages={{ shell: { loadingWorkspace: 'Loading' } }}><RouteLoader /></NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

describe('RouteLoader on dashboard routes', () => {
  it('closes once the readiness marker exists even while gadget queries fetch', async () => {
    vi.useFakeTimers();
    renderLoader();
    expect(screen.getByRole('status')).toBeTruthy();
    const marker = document.createElement('div');
    marker.setAttribute('data-dashboard-ready', 'true');
    document.body.appendChild(marker);
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(screen.queryByRole('status')).toBeNull();
    marker.remove();
    vi.useRealTimers();
  });
});

describe('RouteLoader on non-dashboard routes', () => {
  it('closes within the arrival cap even while a polling query keeps fetching', async () => {
    vi.useFakeTimers();
    route.path = '/news';
    renderLoader();
    expect(screen.getByRole('status')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('status')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(400); });
    expect(screen.queryByRole('status')).toBeNull();
    route.path = '/dashboard';
    vi.useRealTimers();
  });
});
