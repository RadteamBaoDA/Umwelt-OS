'use client';

import { ChevronRight } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { OperationsPage } from '@/modules/observability/operations-page';
import { SystemStatusDashboard } from '@/modules/observability/system-status-dashboard';
import { LifecycleSettings } from '@/modules/settings/lifecycle-settings';
import { DataExportSettings } from '@/modules/settings/data-export';

/** Composes the operations dashboard with the collapsible advanced operations/lifecycle panels and data export. */
function SystemStatus() {
  const tOperations = useTranslations('observability');
  return <div className="flex flex-col gap-5">
    <SystemStatusDashboard />
    <details className="group mt-3 border-t border-border pt-6">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-semibold [&::-webkit-details-marker]:hidden"><ChevronRight aria-hidden="true" className="size-4 transition-transform group-open:rotate-90" />{tOperations('advancedTitle')}</summary>
      <div className="mt-5"><OperationsPage /></div>
      <div className="mt-5"><LifecycleSettings /></div>
    </details>
    <div className="max-w-prose"><DataExportSettings /></div>
  </div>;
}

/** Renders system status within the workspace shell. */
export default function SystemPage() { return <SystemStatus />; }
