import { Suspense } from 'react';
import { NewsPage } from '@/modules/news';

/** Renders the News stories page in the workspace shell. */
export default function Page() {
  return (
    <Suspense fallback={null}>
      <NewsPage />
    </Suspense>
  );
}
