import { Suspense } from 'react';
import { TimelinePage } from '@/modules/timeline/timeline-page';

/** Renders the Timeline feature in the shared workspace shell while URL state resolves. */
export default function Page() {
  return <Suspense fallback={<div className="rounded-lg border bg-card p-6 skeleton" />}><TimelinePage /></Suspense>;
}
