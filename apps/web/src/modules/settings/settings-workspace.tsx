'use client';

import { useTranslations } from 'next-intl';
import { SettingsPageHeader } from './settings-layout';
import { SourceList } from '@/modules/sources/source-list';
import { OperationsPage } from '@/modules/observability/operations-page';
import { LifecycleSettings } from '@/modules/settings/lifecycle-settings';
import { DataExportSettings } from '@/modules/settings/data-export';

/** Renders source management and owner interests in the authenticated workspace. */
export function SettingsWorkspace() {
  const t = useTranslations('sources');
  const tShell = useTranslations('shell');
  const tOperations = useTranslations('observability');
  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={tShell('settings')} title={t('title')} description={t('description')} />
    <SourceList />
    <details className="mt-8 border-t border-border pt-6">
      <summary className="cursor-pointer text-lg font-semibold">{tOperations('advancedTitle')}</summary>
      <div className="mt-5"><OperationsPage /></div>
      <div className="mt-5"><LifecycleSettings /></div>
    </details>
    <DataExportSettings />
  </div>;
}
