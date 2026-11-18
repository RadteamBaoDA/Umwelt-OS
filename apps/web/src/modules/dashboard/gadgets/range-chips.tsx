'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { useSkin } from '@/core/use-skin';
import { CHART_RANGES, type ChartRangeId } from './chart-range';

/**
 * Labelled group of range toggle buttons (`24h 7D 30D 90D 1Y ALL`) for a single-series chart. Exactly one is pressed
 * (`aria-pressed`). The terminal skin draws them as `[x] 7D` chips (CSS `.range-chips`), the default skin as small
 * shadcn buttons. Session-local: selection is reported to the caller and never saved.
 *
 * @param props `value` the pressed chip and `onChange` called with the chosen chip id.
 * @returns The chip group.
 */
export function RangeChips({ value, onChange }: { value: ChartRangeId; onChange: (id: ChartRangeId) => void }) {
  const t = useTranslations('dashboard');
  const terminal = useSkin() === 'terminal';
  return (
    <div role="group" aria-label={t('providerObs.rangeLabel')} data-testid="range-chips" className="range-chips flex flex-wrap gap-1">
      {CHART_RANGES.map(({ id }) =>
        terminal ? (
          <button key={id} type="button" aria-pressed={id === value} className="range-chip" onClick={() => onChange(id)}>
            {id}
          </button>
        ) : (
          <Button
            key={id}
            type="button"
            size="sm"
            variant={id === value ? 'secondary' : 'ghost'}
            aria-pressed={id === value}
            className="min-h-7 px-2 py-0.5 text-xs"
            onClick={() => onChange(id)}
          >
            {id}
          </Button>
        ),
      )}
    </div>
  );
}
