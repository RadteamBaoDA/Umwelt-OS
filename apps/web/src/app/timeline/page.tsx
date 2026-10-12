import { Suspense } from 'react';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { TimelinePage } from '@/modules/timeline/timeline-page';

/** Renders the Timeline feature in the shared workspace shell while URL state resolves. */
export default function Page() {
  return <WorkspaceShell><Suspense fallback={<div className="rounded-xl border bg-card p-6 skeleton" />}><TimelinePage /></Suspense></WorkspaceShell>;
}
