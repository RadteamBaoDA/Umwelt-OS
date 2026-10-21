'use client';

import { useWorkspace } from '@/core/workspace-context';
import { DailyBrief } from './daily-brief';
import { DashboardPage } from './dashboard-page';

/** Shows shared briefs to invited members and the dashboard to owners and read-only administrators. */
export function AppHome() {
  const { selection } = useWorkspace();
  if (selection?.role === 'member') return <section className="min-h-[24rem] bg-card rounded-lg border border-border"><DailyBrief /></section>;
  return <DashboardPage />;
}
