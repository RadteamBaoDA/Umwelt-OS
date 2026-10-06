'use client';

import { useTranslations } from 'next-intl';
import { SourceList } from '@/modules/sources/source-list';
import { OperationsPage } from '@/modules/observability/operations-page';
import { LifecycleSettings } from '@/modules/settings/lifecycle-settings';
import { BackupExportSettings } from '@/modules/settings/backup-export';

/** Renders source management and owner interests in the authenticated workspace. */
export function SettingsWorkspace() {
  const t = useTranslations('sources');
  const tOperations = useTranslations('observability');
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
    <BackupExportSettings />
  </section>;
}
