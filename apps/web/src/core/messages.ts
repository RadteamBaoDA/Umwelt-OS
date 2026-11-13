/* eslint-disable no-restricted-imports -- message catalogs are imported by path on purpose: the module index files pull in components that import this barrel (import cycle). */
import mcpCatalog from '@/modules/sources/mcp-messages.json';
import { storyMessages } from '@/modules/news/story-messages';
import { pickMessages } from '@/modules/pick/messages';
import { automationMessages } from '@/modules/automations/messages';
import { observabilityMessages } from '@/modules/observability/messages';
import { exportMessages } from '@/modules/settings/export-messages';
import { onboardingMessages } from './messages/onboarding';
import { setupMessages } from './messages/setup';
import { topicsMessages } from './messages/topics';
import { timelineMessages } from './messages/timeline';
import { preferencesMessages } from './messages/preferences';
import { loginMessages } from './messages/login';
import { accountMessages } from './messages/account';
import { accountDeletionMessages } from './messages/account-deletion';
import { jsonCatalogs } from '@locales';
import { sourcesMessages } from './messages/sources';
import { memoryMessages } from './messages/memory';
import { memoryPrivacyMessages } from './messages/memory-privacy';
import { aiSettingsMessages } from './messages/ai-settings';
import { systemPromptsMessages } from './messages/system-prompts';
import { webSearchMessages } from './messages/web-search';
import { vizRouterMessages } from './messages/viz-router';
import { todosMessages } from './messages/todos';
import { n8nMessages } from './messages/n8n';
import { searchEntitiesMessages } from './messages/search';
import { detailMessages } from './messages/detail';
import { documentsMessages } from './messages/documents';
import { workspacesMessages } from './messages/workspaces';
import { workspaceMembersMessages } from './messages/workspace-members';
import { sharingMessages } from './messages/sharing';
import { translationsMessages } from './messages/translations';
import { routeErrorsMessages } from './messages/route-errors';
import { errorCodesMessages } from './messages/error-codes';
import { globalSettingsMessages } from './messages/global-settings';
import { landingMessages } from './messages/landing';

/**
 * Compose every locale from per-area fragments in ./messages/<area>.ts.
 * Namespaces already migrated to JSON (locales/<locale>/<ns>.json) come from `jsonCatalogs`. Each UI wave edits only its own fragment; add a new area by creating a fragment file and one line per locale below.
 */
export const messages = {
  'en-us': {
    shell: jsonCatalogs['en-us'].shell,
    landing: landingMessages['en-us'],
    onboarding: onboardingMessages['en-us'],
    setup: setupMessages['en-us'],
    topics: topicsMessages['en-us'],
    timeline: timelineMessages['en-us'],
    preferences: preferencesMessages['en-us'],
    login: loginMessages['en-us'],
    account: accountMessages['en-us'],
    accountDeletion: accountDeletionMessages['en-us'],
    common: jsonCatalogs['en-us'].common,
    sources: sourcesMessages['en-us'],
    chat: jsonCatalogs['en-us'].chat,
    memory: memoryMessages['en-us'],
    memoryPrivacy: memoryPrivacyMessages['en-us'],
    aiSettings: aiSettingsMessages['en-us'],
    systemPrompts: systemPromptsMessages['en-us'],
    webSearch: webSearchMessages['en-us'],
    vizRouter: vizRouterMessages['en-us'],
    todos: todosMessages['en-us'],
    n8n: n8nMessages['en-us'],
    dashboard: jsonCatalogs['en-us'].dashboard,
    gadgetSettings: jsonCatalogs['en-us'].gadgetSettings,
    taskGoal: jsonCatalogs['en-us'].taskGoal,
    entities: searchEntitiesMessages['en-us'],
    observability: observabilityMessages['en-us'],
    exports: exportMessages['en-us'],
    mcp: mcpCatalog['en-us'],
    detail: detailMessages['en-us'],
    documents: documentsMessages['en-us'],
    news: storyMessages['en-us'],
    daily: jsonCatalogs['en-us'].daily,
    github: jsonCatalogs['en-us'].github,
    automations: automationMessages['en-us'],
    notifications: jsonCatalogs['en-us'].notifications,
    pick: pickMessages['en-us'],
    workspaces: workspacesMessages['en-us'],
    globalSettings: globalSettingsMessages['en-us'],
    errorCodes: errorCodesMessages['en-us'],
    workspaceMembers: workspaceMembersMessages['en-us'],
    sharing: sharingMessages['en-us'],
    translations: translationsMessages['en-us'],
    uiSweep: jsonCatalogs['en-us'].uiSweep,
    routeErrors: routeErrorsMessages['en-us'],
  },
  'vi-vi': {
    shell: jsonCatalogs['vi-vi'].shell,
    landing: landingMessages['vi-vi'],
    onboarding: onboardingMessages['vi-vi'],
    setup: setupMessages['vi-vi'],
    topics: topicsMessages['vi-vi'],
    timeline: timelineMessages['vi-vi'],
    preferences: preferencesMessages['vi-vi'],
    login: loginMessages['vi-vi'],
    account: accountMessages['vi-vi'],
    accountDeletion: accountDeletionMessages['vi-vi'],
    common: jsonCatalogs['vi-vi'].common,
    sources: sourcesMessages['vi-vi'],
    chat: jsonCatalogs['vi-vi'].chat,
    memory: memoryMessages['vi-vi'],
    memoryPrivacy: memoryPrivacyMessages['vi-vi'],
    aiSettings: aiSettingsMessages['vi-vi'],
    systemPrompts: systemPromptsMessages['vi-vi'],
    webSearch: webSearchMessages['vi-vi'],
    vizRouter: vizRouterMessages['vi-vi'],
    todos: todosMessages['vi-vi'],
    n8n: n8nMessages['vi-vi'],
    dashboard: jsonCatalogs['vi-vi'].dashboard,
    gadgetSettings: jsonCatalogs['vi-vi'].gadgetSettings,
    taskGoal: jsonCatalogs['vi-vi'].taskGoal,
    entities: searchEntitiesMessages['vi-vi'],
    observability: observabilityMessages['vi-vi'],
    exports: exportMessages['vi-vi'],
    mcp: mcpCatalog['vi-vi'],
    detail: detailMessages['vi-vi'],
    documents: documentsMessages['vi-vi'],
    news: storyMessages['vi-vi'],
    daily: jsonCatalogs['vi-vi'].daily,
    github: jsonCatalogs['vi-vi'].github,
    automations: automationMessages['vi-vi'],
    notifications: jsonCatalogs['vi-vi'].notifications,
    pick: pickMessages['vi-vi'],
    workspaces: workspacesMessages['vi-vi'],
    globalSettings: globalSettingsMessages['vi-vi'],
    errorCodes: errorCodesMessages['vi-vi'],
    workspaceMembers: workspaceMembersMessages['vi-vi'],
    sharing: sharingMessages['vi-vi'],
    translations: translationsMessages['vi-vi'],
    uiSweep: jsonCatalogs['vi-vi'].uiSweep,
    routeErrors: routeErrorsMessages['vi-vi'],
  },
} as const;
