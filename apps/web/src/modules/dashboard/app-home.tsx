'use client';

import { useWorkspace } from '@/core/workspace-context';
import { DailyBrief } from './daily-brief';
import { DashboardPage } from './dashboard-page';

/** Shows shared briefs to members or the optionally route-selected dashboard to other workspace roles.
 * @param dashboardId Dashboard selected by `/dashboard/<dashboard-id>`; absent on the `/dashboard` entry.
 */
export function AppHome({ dashboardId }: { dashboardId?: string }) {
  const { selection } = useWorkspace();
  if (selection?.role === 'member') return <section className="min-h-[24rem] bg-card rounded-lg border border-border"><DailyBrief /></section>;
  return <DashboardPage dashboardId={dashboardId} />;
}
