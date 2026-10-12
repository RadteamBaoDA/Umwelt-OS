import { Suspense } from 'react';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { SearchPage } from '@/modules/search/search-page';

/** Renders search in the workspace shell with a loading fallback for suspended content. */
export default function Page() { return <WorkspaceShell><Suspense fallback={<div className="rounded-xl border bg-card p-6 skeleton" aria-label="Loading search" />}><SearchPage /></Suspense></WorkspaceShell>; }
