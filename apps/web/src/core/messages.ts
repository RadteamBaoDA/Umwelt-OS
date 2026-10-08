import mcpCatalog from '@/modules/sources/mcp-messages.json';
import { storyMessages } from '@/modules/news/story-messages';
import { dailyMessages, notificationMessages } from '@/modules/dashboard/daily-messages';
import { githubMessages } from '@/modules/dashboard/github-messages';
import { automationMessages } from '@/modules/automations/messages';
import { observabilityMessages } from '@/modules/observability/messages';
import { exportMessages } from '@/modules/settings/export-messages';
import { shellMessages } from './messages/shell';
import { onboardingMessages } from './messages/onboarding';
import { setupMessages } from './messages/setup';
import { topicsMessages } from './messages/topics';
import { timelineMessages } from './messages/timeline';
import { preferencesMessages } from './messages/preferences';
import { loginMessages } from './messages/login';
import { accountMessages } from './messages/account';
import { commonMessages } from './messages/common';
import { sourcesMessages } from './messages/sources';
import { chatMessages } from './messages/chat';
import { memoryMessages } from './messages/memory';
import { memoryPrivacyMessages } from './messages/memory-privacy';
import { aiSettingsMessages } from './messages/ai-settings';
import { dashboardMessages } from './messages/dashboard';
import { gadgetSettingsMessages } from './messages/gadget-settings';
import { taskGoalMessages } from './messages/task-goal';
import { searchEntitiesMessages } from './messages/search';
import { detailMessages } from './messages/detail';
import { documentsMessages } from './messages/documents';

/**
 * Compose every locale from per-area fragments in ./messages/<area>.ts.
 * Each UI wave edits only its own fragment; add a new area by creating a fragment file and one line per locale below.
 */
export const messages = {
  'en-us': {
    shell: shellMessages['en-us'],
    onboarding: onboardingMessages['en-us'],
    setup: setupMessages['en-us'],
    topics: topicsMessages['en-us'],
    timeline: timelineMessages['en-us'],
    preferences: preferencesMessages['en-us'],
    login: loginMessages['en-us'],
    account: accountMessages['en-us'],
    common: commonMessages['en-us'],
    sources: sourcesMessages['en-us'],
    chat: chatMessages['en-us'],
    memory: memoryMessages['en-us'],
    memoryPrivacy: memoryPrivacyMessages['en-us'],
    aiSettings: aiSettingsMessages['en-us'],
    dashboard: dashboardMessages['en-us'],
    gadgetSettings: gadgetSettingsMessages['en-us'],
    taskGoal: taskGoalMessages['en-us'],
    entities: searchEntitiesMessages['en-us'],
    observability: observabilityMessages['en-us'],
    exports: exportMessages['en-us'],
    mcp: mcpCatalog['en-us'],
    detail: detailMessages['en-us'],
    documents: documentsMessages['en-us'],
    news: storyMessages['en-us'],
    daily: dailyMessages['en-us'],
    github: githubMessages['en-us'],
    automations: automationMessages['en-us'],
    notifications: notificationMessages['en-us'],
  },
  'vi-vi': {
    shell: shellMessages['vi-vi'],
    onboarding: onboardingMessages['vi-vi'],
    setup: setupMessages['vi-vi'],
    topics: topicsMessages['vi-vi'],
    timeline: timelineMessages['vi-vi'],
    preferences: preferencesMessages['vi-vi'],
    login: loginMessages['vi-vi'],
    account: accountMessages['vi-vi'],
    common: commonMessages['vi-vi'],
    sources: sourcesMessages['vi-vi'],
    chat: chatMessages['vi-vi'],
    memory: memoryMessages['vi-vi'],
    memoryPrivacy: memoryPrivacyMessages['vi-vi'],
    aiSettings: aiSettingsMessages['vi-vi'],
    dashboard: dashboardMessages['vi-vi'],
    gadgetSettings: gadgetSettingsMessages['vi-vi'],
    taskGoal: taskGoalMessages['vi-vi'],
    entities: searchEntitiesMessages['vi-vi'],
    observability: observabilityMessages['vi-vi'],
    exports: exportMessages['vi-vi'],
    mcp: mcpCatalog['vi-vi'],
    detail: detailMessages['vi-vi'],
    documents: documentsMessages['vi-vi'],
    news: storyMessages['vi-vi'],
    daily: dailyMessages['vi-vi'],
    github: githubMessages['vi-vi'],
    automations: automationMessages['vi-vi'],
    notifications: notificationMessages['vi-vi'],
  },
} as const;
