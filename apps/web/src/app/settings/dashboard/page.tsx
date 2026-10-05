'use client';

import { useTranslations } from 'next-intl';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { GadgetEditor } from '@/modules/settings/gadget-editor';

/** Renders dashboard and gadget settings within the workspace shell. */
export default function DashboardSettingsPage() {
  const t = useTranslations('shell');
  return (
    <WorkspaceShell>
      <section className="content-panel space-y-6">
        <div>
          <span className="brand">{t('dashboardSettingsTitle')}</span>
          <h1>{t('dashboardSettingsTitle')}</h1>
          <p className="muted">{t('dashboardSettingsIntro')}</p>
        </div>
        <GadgetEditor />
      </section>
    </WorkspaceShell>
  );
}
