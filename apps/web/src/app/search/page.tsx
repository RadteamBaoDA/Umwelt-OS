'use client';

import { Suspense } from 'react';
import { useTranslations } from 'next-intl';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { SearchPage } from '@/modules/search/search-page';

/** Renders search in the workspace shell with a loading fallback for suspended content. */
export default function Page() { const ts = useTranslations('uiSweep'); return <WorkspaceShell><Suspense fallback={<div className="content-panel skeleton" aria-label={ts('loadingSearch')} />}><SearchPage /></Suspense></WorkspaceShell>; }
