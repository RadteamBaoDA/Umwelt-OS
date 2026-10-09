'use client';

import { useWorkspace } from '@/core/workspace-context';
import { DailyBrief } from './daily-brief';
import { DashboardPage } from './dashboard-page';

/** `/app` home: owners get the dashboard; invited members (403 on dashboards) get the Daily Brief shared with them. */
export function AppHome() {
  const { selection, isOwner } = useWorkspace();
  if (selection && !isOwner) return <section className="min-h-[24rem] bg-card rounded-lg border border-border"><DailyBrief /></section>;
  return <DashboardPage />;
}
