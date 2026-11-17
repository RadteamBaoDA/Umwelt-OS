import { describe, expect, it } from 'vitest';
import { navHref } from '@/core/app-shell/shell-nav';

const settings = { id: 'settings', href: '/settings/workspace' } as never;
const dashboard = { id: 'dashboard', href: '/dashboard' } as never;

describe('navHref', () => {
  it('keeps an open dashboard path', () => {
    expect(navHref(dashboard, '/dashboard/abc', 'member', undefined)).toBe('/dashboard/abc');
  });

  it('sends admin viewers to global settings and members to their first visible group', () => {
    expect(navHref(settings, '/chat', 'admin_viewer' as never, undefined)).toBe('/settings/global');
    expect(navHref(settings, '/chat', 'member', '/settings/ai')).toBe('/settings/ai');
  });

  it('falls back to the item href for a member with no visible settings group', () => {
    expect(navHref(settings, '/chat', 'member', undefined)).toBe('/settings/workspace');
  });
});
