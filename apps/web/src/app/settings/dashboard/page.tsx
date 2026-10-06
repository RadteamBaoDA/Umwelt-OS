'use client';

import { useTranslations } from 'next-intl';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { GadgetEditor } from '@/modules/settings/gadget-editor';

/** Renders dashboard and gadget settings within the workspace shell. */
export default function DashboardSettingsPage() {
  const t = useTranslations('gadgetSettings');
  return (
    <WorkspaceShell>
      <section className="content-panel space-y-6">
        <div>
          <span className="brand">{t('title')}</span>
          <h1>{t('title')}</h1>
          <p className="muted">{t('intro')}</p>
        </div>
        <GadgetEditor />
      </section>
    </WorkspaceShell>
  );
}
