import { Suspense } from 'react';
import { SettingsWorkspace } from '@/modules/settings/settings-workspace';

/** Renders data-source settings within the workspace shell; Suspense lets the `?shapes=&for=` filter read search params during static rendering. */
export default function SettingsSourcesPage() { return <Suspense fallback={<div className="rounded-lg border bg-card p-6 skeleton" />}><SettingsWorkspace /></Suspense>; }
