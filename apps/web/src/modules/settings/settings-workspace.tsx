'use client';

import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useTranslations } from 'next-intl';
import { TopicSettings } from '@/modules/news/topic-settings';
import { SourceList } from '@/modules/sources/source-list';
import { McpSettings } from '@/modules/sources/mcp-editor';
import { OperationsPage } from '@/modules/observability/operations-page';
import { LifecycleSettings } from '@/modules/settings/lifecycle-settings';

/** Renders source, MCP connection/grant management, and owner interests in the authenticated workspace. */
export function SettingsWorkspace() {
  const t = useTranslations('sources');
  const tOperations = useTranslations('observability');
  const { csrfToken } = useWorkspaceSession();
  return <section className="content-panel">
    <header className="section-heading settings-group-heading">
      <div><span className="brand">Settings</span><h1>{t('title')}</h1><p className="muted">{t('description')}</p></div>
    </header>
    <SourceList />
    <details className="mt-8 border-t border-border pt-6">
      <summary className="cursor-pointer text-lg font-semibold">{tOperations('advancedTitle')}</summary>
      <div className="mt-5"><OperationsPage /></div>
      <div className="mt-5"><LifecycleSettings /></div>
    </details>
    <div className="mt-8 border-t border-border pt-8">
      <McpSettings />
    </div>
    <div className="mt-8 border-t border-border pt-8">
      <TopicSettings csrfToken={csrfToken} />
    </div>
  </section>;
}
