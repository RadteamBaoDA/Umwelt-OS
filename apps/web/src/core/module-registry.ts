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
  | 'systemStatus';

export type NavigationDestination = {
  id: string;
  href: string;
  messageKey: ShellMessageKey;
};

export type ModuleAvailability = { modules: { id: string; enabled: boolean }[] };

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
  { id: 'dashboard', href: '/app', messageKey: 'dashboard' },
  { id: 'chat', href: '/chat', messageKey: 'chat' },
  { id: 'settings', href: '/settings/sources', messageKey: 'settings' },
];

export const settingsGroups: NavigationDestination[] = [
  { id: 'data-sources', href: '/settings/sources', messageKey: 'dataSources' },
  { id: 'ai-router', href: '/settings/ai', messageKey: 'aiRouter' },
  { id: 'dashboard-gadgets', href: '/settings/dashboard', messageKey: 'dashboardGadgets' },
];

export const detailDestinations: NavigationDestination[] = [
  { id: 'documents', href: '/knowledge/documents', messageKey: 'documents' },
  { id: 'entities', href: '/knowledge/entities', messageKey: 'entities' },
  { id: 'search', href: '/search', messageKey: 'search' },
  { id: 'system', href: '/settings/system', messageKey: 'systemStatus' },
];

export const commandDestinations = [...mainNavigation, ...settingsGroups.slice(1), ...detailDestinations];
