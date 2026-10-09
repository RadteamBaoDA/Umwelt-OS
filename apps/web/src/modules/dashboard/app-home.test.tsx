import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const ws = vi.hoisted(() => ({ value: { selection: { id: 'w', role: 'member' }, isOwner: false } }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ws.value }));
vi.mock('./daily-brief', () => ({ DailyBrief: () => <p>brief</p> }));
vi.mock('./dashboard-page', () => ({ DashboardPage: () => <p>dashboard</p> }));

import { AppHome } from './app-home';

describe('AppHome', () => {
  beforeEach(() => { ws.value = { selection: { id: 'w', role: 'member' }, isOwner: false }; });
  it('shows the shared Daily Brief to members instead of the dashboard', () => {
    render(<AppHome />);
    expect(screen.getByText('brief')).toBeTruthy();
    expect(screen.queryByText('dashboard')).toBeNull();
  });
  it('shows the dashboard to owners', () => {
    ws.value = { selection: { id: 'w', role: 'owner' }, isOwner: true };
    render(<AppHome />);
    expect(screen.getByText('dashboard')).toBeTruthy();
  });
});
