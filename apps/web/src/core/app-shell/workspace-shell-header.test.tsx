import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const toggleDrawer = vi.fn();
const apiRequest = vi.fn();
let mockPath = '/dashboard/abc';

vi.mock('next/navigation', () => ({ useRouter: () => ({ replace: vi.fn() }), usePathname: () => mockPath }));
vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => key }));
vi.mock('@/core/api', async () => {
  class ApiError extends Error {
    constructor(
      readonly status: number,
      message: string,
    ) {
      super(message);
    }
  }
  return { ApiError, apiRequest: (...args: unknown[]) => apiRequest(...args), csrfHeaders: () => ({}), retrySessionConflict: () => false };
});
vi.mock('@/core/query-provider', () => ({
  useDisplayPreferences: () => ({
    authGeneration: 0,
    confirmedPreferences: null,
    confirmPreferences: vi.fn(),
    endAuthSession: vi.fn(),
    isCurrentGeneration: () => true,
  }),
}));
vi.mock('@/core/realtime-provider', () => ({ useRealtime: () => ({ status: 'open' }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ selection: { id: 'w1', role: 'owner', revision: 1 } }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ toggleDrawer }) }));
vi.mock('@/core/command-palette', () => ({ CommandPalette: () => null }));
vi.mock('@/core/app-shell/connection-footer', () => ({ ConnectionFooter: () => null }));
vi.mock('@/core/app-shell/settings-nav', () => ({ SettingsPanel: () => null, SourcesSubNav: () => null, groupActive: () => false }));
vi.mock('@/core/app-shell/workspace-switcher', () => ({ WorkspaceSwitcher: () => <div data-testid="switcher" /> }));
vi.mock('@/modules/account/change-password-dialog', () => ({ ChangePasswordDialog: () => null }));
vi.mock('@/modules/account/google-link', () => ({ GoogleLink: () => null }));
vi.mock('@/modules/account/account-deletion', () => ({ AccountDeletionSection: () => null }));
vi.mock('@/modules/legal/legal-links', () => ({ LegalLinks: () => null }));
vi.mock('@/modules/account/preferences-dialog', () => ({ PreferencesDialog: () => null }));
vi.mock('@/modules/chat/chat-drawer', () => ({ ChatDrawer: () => null }));
vi.mock('@/modules/notifications/notification-bell', () => ({ NotificationBell: () => <div data-testid="bell" /> }));
vi.mock('@/modules/account/user-menu', () => ({ UserMenu: () => <button>avatar</button> }));

import { WorkspaceShell } from '@/core/app-shell/workspace-shell';

describe('WorkspaceShell header', () => {
  beforeEach(() => {
    mockPath = '/dashboard/abc';
    toggleDrawer.mockClear();
    apiRequest.mockReset();
    apiRequest.mockImplementation(async (path: string) =>
      path === '/api/v1/auth/session' ? { authenticated: true, csrfToken: 't', isInstanceAdmin: false } : undefined,
    );
  });

  /** Renders the shell on a dashboard route where the quick-chat shortcut is active. */
  const mount = () =>
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <WorkspaceShell>content</WorkspaceShell>
      </QueryClientProvider>,
    );

  it('has a Quick chat button that toggles the drawer (C13) and keeps Ctrl+J', async () => {
    mount();
    await screen.findByText('avatar');
    fireEvent.click(screen.getByRole('button', { name: 'quickChat' }));
    expect(toggleDrawer).toHaveBeenCalledTimes(1);
    toggleDrawer.mockClear();
    fireEvent.keyDown(window, { key: 'j', ctrlKey: true });
    expect(toggleDrawer).toHaveBeenCalledTimes(1);
  });

  it('registers the quick chat button and Ctrl+J on the dashboard home too', async () => {
    mockPath = '/dashboard';
    mount();
    await screen.findByText('avatar');
    fireEvent.keyDown(window, { key: 'j', ctrlKey: true });
    expect(toggleDrawer).toHaveBeenCalledTimes(1);
    mockPath = '/dashboard/abc';
  });

  it('hides the workspace switcher and bell below 640px (they live in the avatar menu there)', async () => {
    mount();
    await screen.findByText('avatar');
    expect(screen.getByTestId('bell').parentElement?.className).toContain('hidden sm:contents');
    expect(screen.getByTestId('switcher').parentElement?.className).toContain('hidden sm:contents');
  });
});
