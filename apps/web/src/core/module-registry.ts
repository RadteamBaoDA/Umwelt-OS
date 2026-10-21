export type ShellMessageKey =
  | 'dashboard'
  | 'chat'
  | 'settings'
  | 'dataSources'
  | 'aiRouter'
  | 'systemPrompts'
  | 'dashboardGadgets'
  | 'workspaceMembers'
  | 'documents'
  | 'timeline'
  | 'entities'
  | 'search'
  | 'news'
  | 'translationSettings'
  | 'memorySettings'
  | 'webSearchSettings'
  | 'n8nSettings'
  | 'systemStatus'
  | 'sourcesTab'
  | 'mcpTab'
  | 'permissionsTab'
  | 'globalSettings'
  | 'collapseSidebar'
  | 'expandSidebar';

export type NavigationDestination = {
  id: string;
  href: string;
  messageKey: ShellMessageKey;
  /** Visible to invited members (read-only shared surfaces); omitted means owner only. */
  members?: true;
  /** Visible only to the instance administrator. */
  adminOnly?: true;
  /** Readable by an administrator observing another workspace. */
  adminViewer?: true;
};

export type ModuleAvailability = { modules: { id: string; enabled: boolean }[] };

/** Allows member-shared surfaces, a narrower administrator read set, or all owner destinations. */
export function destinationForRole(destination: NavigationDestination, role?: 'owner' | 'member' | 'admin_viewer'): boolean {
  if (destination.adminOnly) return false;
  if (role === 'admin_viewer') return Boolean(destination.adminViewer || destination.members) && !['chat', 'settings', 'translation-settings'].includes(destination.id);
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
  { id: 'system-prompts', href: '/settings/prompts', messageKey: 'systemPrompts' },
  { id: 'dashboard-gadgets', href: '/settings/dashboard', messageKey: 'dashboardGadgets' },
  { id: 'workspace-members', href: '/settings/workspace', messageKey: 'workspaceMembers' },
  { id: 'translation-settings', href: '/settings/translation', messageKey: 'translationSettings', members: true },
];

/** Owner-only settings links shown after the three design groups and Members/Translation in the settings panel nav (not in the main Settings rail or palette). */
export const settingsExtraLinks: NavigationDestination[] = [
  { id: 'global-settings', href: '/settings/global', messageKey: 'globalSettings', adminOnly: true },
  { id: 'memory-settings', href: '/settings/memory', messageKey: 'memorySettings' },
  { id: 'web-search-settings', href: '/settings/web-search', messageKey: 'webSearchSettings' },
  { id: 'n8n-settings', href: '/settings/n8n', messageKey: 'n8nSettings' },
  { id: 'system-status', href: '/settings/system', messageKey: 'systemStatus' },
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
  { id: 'timeline', href: '/timeline', messageKey: 'timeline', adminViewer: true },
  { id: 'entities', href: '/knowledge/entities', messageKey: 'entities', adminViewer: true },
  { id: 'news', href: '/news', messageKey: 'news', members: true },
  { id: 'search', href: '/search', messageKey: 'search', members: true },
  { id: 'system', href: '/settings/system', messageKey: 'systemStatus' },
];

export const commandDestinations = [...mainNavigation, ...settingsGroups.slice(1), ...detailDestinations];
