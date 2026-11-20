'use client';

import { useState } from 'react';
import { useTranslations } from 'next-intl';
import type { TranslationProgress as Progress } from './use-content-translation';

/**
 * Thin determinate bar and one polite completion announcement for a Translate-all run. The bar is absolutely
 * positioned at the bottom edge of its relatively positioned toolbar parent, so it never shifts the toolbar.
 * The sr-only live region is always mounted so that its text change is announced; it is set once when a run
 * completes and is silent for single-item translations, which do not render a progress value.
 *
 * @param props.progress Active run progress, or null when no Translate-all run is settling.
 */
export function TranslationRunProgress({ progress }: { progress: Progress | null }) {
  const t = useTranslations('translations');
  const total = progress?.total ?? null;
  const [announcement, setAnnouncement] = useState('');
  const [lastRunTotal, setLastRunTotal] = useState<number | null>(null);

  // Remember the size of the run while it is active; when it clears, announce the count exactly once. Adjusting state
  // during render (guarded so it converges) avoids an extra effect-driven render pass.
  if (total !== null && total !== lastRunTotal) {
    setLastRunTotal(total);
    setAnnouncement('');
  } else if (total === null && lastRunTotal !== null) {
    setAnnouncement(t('translatedCountAnnouncement', { count: lastRunTotal }));
    setLastRunTotal(null);
  }

  const percent = progress && progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0;
  return (
    <>
      {progress ? (
        <div
          role="progressbar"
          aria-label={t('progressLabel')}
          aria-valuemin={0}
          aria-valuemax={progress.total}
          aria-valuenow={progress.done}
          className="pointer-events-none absolute inset-x-0 -bottom-px h-0.5 overflow-hidden rounded-full bg-line"
        >
          <div
            className="h-full rounded-full bg-accent transition-[width] duration-300 ease-out motion-reduce:transition-none"
            style={{ width: `${percent}%` }}
          />
        </div>
      ) : null}
      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>
    </>
  );
}
