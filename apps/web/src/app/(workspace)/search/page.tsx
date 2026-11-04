'use client';

import { Suspense } from 'react';
import { useTranslations } from 'next-intl';
import { SearchPage } from '@/modules/search/search-page';

/** Renders search in the workspace shell with a loading fallback for suspended content. */
export default function Page() { const ts = useTranslations('uiSweep'); return <Suspense fallback={<div className="rounded-lg border bg-card p-6 skeleton" aria-label={ts('loadingSearch')} />}><SearchPage /></Suspense>; }
