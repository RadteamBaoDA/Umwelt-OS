import type { jsonCatalogs } from '@locales';

// Types next-intl's `useTranslations`/`getTranslations` keys: namespaces migrated to JSON are checked down to the
// nested key (`t('group.key')`); namespaces still in src/core/messages/*.ts stay loosely typed until their wave lands.
type Migrated = (typeof jsonCatalogs)['en-us'];
type LegacyNamespace =
  | 'landing'
  | 'onboarding'
  | 'setup'
  | 'topics'
  | 'timeline'
  | 'preferences'
  | 'login'
  | 'account'
  | 'accountDeletion'
  | 'sources'
  | 'memory'
  | 'memoryPrivacy'
  | 'aiSettings'
  | 'systemPrompts'
  | 'webSearch'
  | 'vizRouter'
  | 'todos'
  | 'n8n'
  | 'entities'
  | 'observability'
  | 'exports'
  | 'mcp'
  | 'detail'
  | 'documents'
  | 'news'
  | 'automations'
  | 'pick'
  | 'workspaces'
  | 'globalSettings'
  | 'errorCodes'
  | 'workspaceMembers'
  | 'sharing'
  | 'translations'
  | 'routeErrors';

declare module 'next-intl' {
  interface AppConfig {
    Messages: Migrated & { [N in LegacyNamespace]: Record<string, any> }; // eslint-disable-line @typescript-eslint/no-explicit-any
  }
}
