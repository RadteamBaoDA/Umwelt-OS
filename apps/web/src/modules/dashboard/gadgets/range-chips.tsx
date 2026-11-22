'use client';

import { useTranslations } from 'next-intl';
import { CHART_RANGES, type ChartRangeId } from './chart-range';

/**
 * Labelled group of range toggle buttons (`24h 7D 30D 90D 1Y ALL`) for a single-series chart. Exactly one is pressed
 * (`aria-pressed`). Both skins draw compact chips (CSS `.range-chip`); the terminal skin adds `[x]` glyphs. Session-local: selection is reported to the caller and never saved.
 *
 * @param props `value` the pressed chip and `onChange` called with the chosen chip id.
 * @returns The chip group.
 */
export function RangeChips({ value, onChange }: { value: ChartRangeId; onChange: (id: ChartRangeId) => void }) {
  const t = useTranslations('dashboard');
  return (
    <div role="group" aria-label={t('providerObs.rangeLabel')} data-testid="range-chips" className="range-chips flex flex-wrap gap-1">
      {CHART_RANGES.map(({ id }) => (
        <button key={id} type="button" aria-pressed={id === value} className="range-chip" onClick={() => onChange(id)}>
          {id}
        </button>
      ))}
    </div>
  );
}
