import { groupActive } from '@/core/app-shell/settings-nav';
import { destinationEnabled, destinationForRole, type ModuleAvailability, type NavigationDestination } from '@/core/module-registry';

type Role = Parameters<typeof destinationForRole>[1];

/** Role-based redirect targets for the current path; each is undefined when no redirect applies. */
export type ShellRedirects = {
  memberSettingsRedirect: string | undefined;
  adminViewerSettingsRedirect: string | undefined;
  adminViewerPrivateRedirect: string | undefined;
};

/**
 * Computes where a role must be sent from the current pathname.
 * A member deep-linking to an owner settings page goes to the first settings group they may see; an admin viewer is
 * confined to global settings (instance admins) or the dashboard and never sees chat or memory.
 *
 * @param role Selected workspace role (undefined while unresolved: no redirect).
 * @param pathname Current route path.
 * @param visibleSettingsGroups Settings groups the role may see, in display order.
 * @param isInstanceAdmin Whether the session user administers the instance.
 */
export function computeShellRedirects(
  role: Role,
  pathname: string,
  visibleSettingsGroups: ReadonlyArray<{ href: string }>,
  isInstanceAdmin: boolean | undefined,
): ShellRedirects {
  const inSettings = pathname.startsWith('/settings');
  const memberSettingsRedirect = role === 'member' && inSettings
    && !visibleSettingsGroups.some((g) => groupActive(pathname, g.href))
    ? visibleSettingsGroups[0]?.href
    : undefined;
  const adminViewerSettingsRedirect = role === 'admin_viewer' && inSettings && pathname !== '/settings/global'
    ? isInstanceAdmin ? '/settings/global' : '/dashboard'
    : undefined;
  const adminViewerPrivateRedirect = role === 'admin_viewer'
    && (pathname === '/chat' || pathname.startsWith('/chat/') || pathname.startsWith('/knowledge/memory'))
    ? '/dashboard'
    : undefined;
  return { memberSettingsRedirect, adminViewerSettingsRedirect, adminViewerPrivateRedirect };
}

/**
 * Filters the main navigation to what the role may reach: module must be enabled, the role must be allowed (instance
 * admins always keep Settings), admin viewers never see Chat, and Settings needs a visible group or instance admin.
 */
export function visibleMainNavigationFor(
  items: ReadonlyArray<NavigationDestination>,
  availability: ModuleAvailability | undefined,
  role: Role,
  settingsGroupCount: number,
  isInstanceAdmin: boolean,
): NavigationDestination[] {
  return items.filter((item) => destinationEnabled(item, availability)
    && (destinationForRole(item, role) || item.id === 'settings' && isInstanceAdmin)
    && !(role === 'admin_viewer' && item.id === 'chat')
    && (item.id !== 'settings' || settingsGroupCount > 0 || isInstanceAdmin));
}

/** Id of the destination that owns the pathname ('settings' for any /settings path, '' when none matches). */
export function activeNavigationId(items: ReadonlyArray<NavigationDestination>, pathname: string): string {
  const match = items.find((item) => item.id !== 'settings'
    && ((item.id === 'dashboard' && pathname.startsWith('/dashboard/')) || pathname === item.href || pathname.startsWith(`${item.href}/`)));
  return match?.id ?? (pathname.startsWith('/settings') ? 'settings' : '');
}
