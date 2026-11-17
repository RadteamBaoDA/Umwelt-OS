import { describe, expect, it } from 'vitest';
import { mainNavigation } from '@/core/module-registry';
import { activeNavigationId, computeShellRedirects, visibleMainNavigationFor } from '@/core/app-shell/shell-redirects';

const groups = [{ href: '/settings/translation' }, { href: '/settings/ai' }];

describe('computeShellRedirects', () => {
  it('sends a member on an unavailable settings page to the first visible group', () => {
    expect(computeShellRedirects('member', '/settings/n8n', groups, false).memberSettingsRedirect).toBe('/settings/translation');
    expect(computeShellRedirects('member', '/settings/ai', groups, false).memberSettingsRedirect).toBeUndefined();
    expect(computeShellRedirects('member', '/dashboard', groups, false).memberSettingsRedirect).toBeUndefined();
  });

  it('confines admin viewers', () => {
    expect(computeShellRedirects('admin_viewer', '/settings/ai', groups, true).adminViewerSettingsRedirect).toBe('/settings/global');
    expect(computeShellRedirects('admin_viewer', '/settings/ai', groups, false).adminViewerSettingsRedirect).toBe('/dashboard');
    expect(computeShellRedirects('admin_viewer', '/settings/global', groups, true).adminViewerSettingsRedirect).toBeUndefined();
    expect(computeShellRedirects('admin_viewer', '/chat/abc', groups, true).adminViewerPrivateRedirect).toBe('/dashboard');
    expect(computeShellRedirects('admin_viewer', '/knowledge/memory', groups, true).adminViewerPrivateRedirect).toBe('/dashboard');
    expect(computeShellRedirects('owner', '/chat', groups, true).adminViewerPrivateRedirect).toBeUndefined();
  });
});

describe('navigation filters', () => {
  it('hides chat from admin viewers and settings from members without groups', () => {
    const ids = (role: 'owner' | 'member' | 'admin_viewer', count: number, admin: boolean) =>
      visibleMainNavigationFor(mainNavigation, undefined, role, count, admin).map((item) => item.id);
    expect(ids('admin_viewer', 0, false)).not.toContain('chat');
    expect(ids('owner', 0, false)).not.toContain('settings');
    expect(ids('owner', 0, true)).toContain('settings');
  });

  it('marks settings and the longest matching destination active', () => {
    expect(activeNavigationId(mainNavigation, '/settings/ai')).toBe('settings');
    expect(activeNavigationId(mainNavigation, '/dashboard/abc')).toBe('dashboard');
    expect(activeNavigationId(mainNavigation, '/unknown')).toBe('');
  });
});
