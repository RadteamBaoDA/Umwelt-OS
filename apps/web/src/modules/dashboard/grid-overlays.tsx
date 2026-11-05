'use client';

import { useTranslations } from 'next-intl';
import React from 'react';
import { GRID_GAP_PX } from './grid-geometry';

/** Props of {@link GridNotices}. */
interface GridNoticesProps {
  isEditMode: boolean;
  /** Live-region text for assistive technology (gesture position or the last keyboard announcement). */
  liveText: string;
  interactionNotice: string | null;
  projectionPending: boolean;
  projectionUnavailable: boolean;
}

/** Screen-reader live region plus the transient overlay notices (interaction, projection pending / unavailable). */
export function GridNotices({ isEditMode, liveText, interactionNotice, projectionPending, projectionUnavailable }: GridNoticesProps) {
  const t = useTranslations('dashboard');
  return (
    <>
      {isEditMode && <p role="status" aria-live="polite" className="sr-only">{liveText}</p>}
      {interactionNotice && (
        <p role="status" className="absolute inset-x-0 top-0 z-20 rounded-md bg-card/95 px-3 py-2 text-sm text-destructive">
          {interactionNotice}
        </p>
      )}
      {projectionPending && (
        <p role="status" className="absolute inset-x-0 top-0 z-20 rounded-md bg-card/95 px-3 py-2 text-sm text-muted-foreground">
          {t('mobileProjectionPending')}
        </p>
      )}
      {projectionUnavailable && (
        <p role="status" className="absolute inset-x-0 top-0 z-20 rounded-md bg-card/95 px-3 py-2 text-sm text-destructive">
          {t('mobileLayoutUnavailable')}
        </p>
      )}
    </>
  );
}

/**
 * Edit-mode backdrop: the numbered column ruler and the dashed unit guides.
 * Guide rows are capped at 200 so a placement with a huge y coordinate (up to the row bound) cannot allocate
 * millions of DOM nodes.
 */
export function GridBackdrop({ columnCount, cellSize, maxRow }: { columnCount: number; cellSize: number; maxRow: number }) {
  const renderRows = Math.min(maxRow, 200);
  return (
    <>
      {/* Column ruler (DashboardEdit artboard): numbers each unit track so sizes read in cells. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-5 left-0 grid text-[11px] leading-none text-muted-foreground"
        style={{ gridTemplateColumns: `repeat(${columnCount}, ${cellSize}px)`, gap: `${GRID_GAP_PX}px` }}
      >
        {Array.from({ length: columnCount }).map((_, i) => (
          <span key={i} className="text-center tabular-nums">{i + 1}</span>
        ))}
      </div>
      {/* Background grid guides in edit mode */}
      <div
        aria-hidden="true"
        className="absolute inset-0 pointer-events-none rounded-lg border border-dashed border-border/40 grid"
        style={{
          gridTemplateColumns: `repeat(${columnCount}, ${cellSize}px)`,
          gridAutoRows: `${cellSize}px`,
          gap: `${GRID_GAP_PX}px`,
        }}
      >
        {Array.from({ length: columnCount * renderRows }).map((_, i) => (
          <div
            key={i}
            className="border border-border/20 rounded-md bg-primary/[0.015]"
          />
        ))}
      </div>
    </>
  );
}
