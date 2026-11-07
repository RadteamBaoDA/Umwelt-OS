'use client';

import { useLayoutEffect, useRef, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';

/** A full-width tab panel whose content scrolls without moving the dashboard header or tab row. */
export function DashboardPanel({ panelId, children, empty, canAdd, onAdd, scrollPositions }: {
  panelId: string;
  children: ReactNode;
  empty: boolean;
  canAdd: boolean;
  onAdd: () => void;
  scrollPositions: Map<string, number>;
}) {
  const t = useTranslations('dashboard');
  const scrollRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollPositions.get(panelId) ?? 0;
  }, [panelId, scrollPositions]);
  return (
    <div ref={scrollRef} role="region" aria-label={t('panelContent')} onScroll={(event) => {
      scrollPositions.set(panelId, event.currentTarget.scrollTop);
    }} className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-y-contain">
      {empty ? (
        <div className="flex min-h-64 flex-col items-center justify-center gap-4 rounded-lg border border-dashed border-border bg-card px-6 text-center">
          <p className="text-sm text-muted-foreground">{t('emptyPanel')}</p>
          {canAdd && <Button type="button" onClick={onAdd}>{t('addGadget')}</Button>}
        </div>
      ) : children}
    </div>
  );
}
