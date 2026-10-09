export type ShellMessageKey =
  | 'dashboard'
  | 'chat'
  | 'settings'
  | 'dataSources'
  | 'aiRouter'
  | 'dashboardGadgets'
  | 'documents'
  | 'entities'
  | 'search'
  | 'systemStatus'
  | 'sourcesTab'
  | 'mcpTab'
  | 'permissionsTab';

export type NavigationDestination = {
  id: string;
  href: string;
  messageKey: ShellMessageKey;
  /** Visible to invited members (read-only shared surfaces); omitted means owner only. */
  members?: true;
};

export type ModuleAvailability = { modules: { id: string; enabled: boolean }[] };

/** Members of an invited workspace see only destinations flagged `members`; owners (and an unresolved selection) see all. */
export function destinationForRole(destination: NavigationDestination, role?: 'owner' | 'member'): boolean {
  return role !== 'member' || Boolean(destination.members);
}

/** Hide a navigation destination when its owning persisted module is unavailable. */
export function destinationEnabled(destination: NavigationDestination, state?: ModuleAvailability): boolean {
  if (!state) return true;
  const owner: Record<string, string> = {
    dashboard: 'dashboard', chat: 'chat', 'data-sources': 'sources',
    'dashboard-gadgets': 'dashboard', documents: 'knowledge.documents',
    entities: 'knowledge.entities', search: 'search',
  };
  const moduleId = owner[destination.id];
  return !moduleId || state.modules.some((module) => module.id === moduleId && module.enabled);
}

export const mainNavigation: NavigationDestination[] = [
  { id: 'dashboard', href: '/app', messageKey: 'dashboard', members: true },
  { id: 'chat', href: '/chat', messageKey: 'chat', members: true },
  { id: 'settings', href: '/settings/sources', messageKey: 'settings', members: true },
];

export const settingsGroups: NavigationDestination[] = [
  { id: 'data-sources', href: '/settings/sources', messageKey: 'dataSources' },
  { id: 'ai-router', href: '/settings/ai', messageKey: 'aiRouter' },
  { id: 'dashboard-gadgets', href: '/settings/dashboard', messageKey: 'dashboardGadgets' },
];

/** Sub-tabs of the Data sources group (rendered by the shell above the page); exact tabs match only their own path. */
export const sourcesSubNavigation: (NavigationDestination & { exact?: boolean })[] = [
  { id: 'sources-list', href: '/settings/sources', messageKey: 'sourcesTab', exact: true },
  { id: 'sources-mcp', href: '/settings/sources/mcp', messageKey: 'mcpTab' },
  { id: 'sources-permissions', href: '/settings/sources/permissions', messageKey: 'permissionsTab' },
];

/** Routes kept reachable from the command palette only (not shown in the Settings rail). */
export const detailDestinations: NavigationDestination[] = [
  { id: 'documents', href: '/knowledge/documents', messageKey: 'documents', members: true },
  { id: 'entities', href: '/knowledge/entities', messageKey: 'entities' },
  { id: 'search', href: '/search', messageKey: 'search', members: true },
  { id: 'system', href: '/settings/system', messageKey: 'systemStatus' },
];

export const commandDestinations = [...mainNavigation, ...settingsGroups.slice(1), ...detailDestinations];
