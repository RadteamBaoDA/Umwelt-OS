import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { DashboardOverview } from './dashboard-overview';

vi.mock('./gadgets/live-map-gadget', () => ({ LiveMapOverview: ({ toolbarStart }: { toolbarStart?: React.ReactNode }) => <div data-testid="map">{toolbarStart}</div> }));
vi.mock('./dashboard-panel', () => ({ DashboardPanel: ({ children }: { children: React.ReactNode }) => <div>{children}</div> }));

/** Renders the overview in desktop (non-compact) mode. */
function show() {
  return render(
    <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}>
      <DashboardOverview panelId="p" editMode={false} compact={false} empty={false} canAdd onAdd={() => {}} scrollPositions={new Map()}>
        <span>gadgets</span>
      </DashboardOverview>
    </NextIntlClientProvider>,
  );
}

describe('DashboardOverview map collapse', () => {
  beforeEach(() => localStorage.clear());

  it('collapses, persists and restores the collapsed state', async () => {
    const { unmount } = show();
    const collapse = screen.getByRole('button', { name: 'Collapse map' });
    expect(collapse.getAttribute('aria-expanded')).toBe('true');
    expect(collapse.getAttribute('aria-controls')).toBe('dashboard-overview-map');
    await userEvent.click(collapse);
    expect(screen.queryByTestId('map')).toBeNull();
    expect(localStorage.getItem('umwelt.dashboard.mapCollapsed')).toBe('1');
    unmount();
    show();
    const expand = await screen.findByRole('button', { name: 'Expand map' });
    expect(expand.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByTestId('map')).toBeNull();
    await userEvent.click(expand);
    expect(screen.getByTestId('map')).toBeTruthy();
    expect(localStorage.getItem('umwelt.dashboard.mapCollapsed')).toBe('0');
  });
});
