'use client';

import { LayoutGrid, List } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';

/** Item stream layouts offered by the feed toggle. */
export const FEED_VIEWS = ['list', 'cards'] as const;
export type FeedView = (typeof FEED_VIEWS)[number];

/**
 * List | Cards icon toggle shared by the Feed and News story gadgets.
 *
 * @param props Current view and its setter (callers persist it, e.g. with useLocalPref).
 */
export function FeedViewToggle({ view, setView }: { view: FeedView; setView: (view: FeedView) => void }) {
  const t = useTranslations('dashboard');
  return (
    <div role="group" aria-label={t('feedViewLabel')} className="flex items-center">
      {FEED_VIEWS.map((mode) => (
        <Button
          key={mode}
          type="button"
          variant={view === mode ? 'default' : 'ghost'}
          size="icon"
          className="size-8"
          aria-pressed={view === mode}
          aria-label={mode === 'list' ? t('feedViewList') : t('feedViewCards')}
          onClick={() => setView(mode)}
        >{mode === 'list' ? <List className="h-3.5 w-3.5" aria-hidden="true" /> : <LayoutGrid className="h-3.5 w-3.5" aria-hidden="true" />}</Button>
      ))}
    </div>
  );
}
