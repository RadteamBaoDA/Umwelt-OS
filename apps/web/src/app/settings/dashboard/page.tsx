'use client';

import { useTranslations } from 'next-intl';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { SettingsPageHeader } from '@/modules/settings/settings-layout';
import { GadgetEditor } from '@/modules/settings/gadget-editor';

/** Renders dashboard and gadget settings within the workspace shell. */
export default function DashboardSettingsPage() {
  const t = useTranslations('gadgetSettings');
  return (
    <WorkspaceShell>
      <div className="flex flex-col gap-5">
        <SettingsPageHeader eyebrow={t('eyebrow')} title={t('title')} description={t('intro')} />
        <GadgetEditor />
      </div>
    </WorkspaceShell>
  );
}
