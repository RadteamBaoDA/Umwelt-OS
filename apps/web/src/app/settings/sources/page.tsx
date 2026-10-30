import { Suspense } from 'react';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { SettingsWorkspace } from '@/modules/settings/settings-workspace';

/** Renders data-source settings within the workspace shell; Suspense lets the `?shapes=&for=` filter read search params during static rendering. */
export default function SettingsSourcesPage() { return <WorkspaceShell><Suspense fallback={<div className="rounded-xl border bg-card p-6 skeleton" />}><SettingsWorkspace /></Suspense></WorkspaceShell>; }
