'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { dashboardKeys, listGadgetSources } from './api';

/** Renderers whose data comes from exactly one configured source, with the provider they require. */
export const SOURCE_BACKED_RENDERERS: Readonly<Record<string, { provider: string }>> = {
  github_project: { provider: 'github' },
  finance_chart: { provider: 'alpha_vantage' },
  weather: { provider: 'open_meteo' },
};

/** Renderers whose configured data sources are read through the Documents owner projection. */
export const MULTI_SOURCE_RENDERERS = new Set([
  'telegram_feed', 'text_panel', 'table_panel', 'video_panel', 'map', 'intelligence_panel',
]);

/** Choose up to 32 active sources without exposing connector configuration. */
export function SourceMultiPicker({ provider, value, onChange, id }: {
  provider?: string;
  value: string[];
  onChange: (sourceIds: string[]) => void;
  id: string;
}) {
  const t = useTranslations('dashboard');
  const query = useQuery({
    queryKey: [...dashboardKeys.sources, 'multi-picker', provider ?? 'all'],
    queryFn: ({ signal }) => listGadgetSources(100, undefined, signal),
  });
  const options = (query.data?.items ?? []).filter((item) =>
    (!provider || item.provider === provider) && (item.status === 'active' || value.includes(item.id)),
  );
  return (
    <fieldset id={id} className="space-y-1.5">
      <legend className="text-xs font-semibold">{t('sourceSelection')}</legend>
      <p className="text-[10px] text-muted-foreground">{t('sourceSelectionLimit')}</p>
      <div className="max-h-36 space-y-1 overflow-y-auto rounded border border-border p-2">
        {options.map((item) => (
          <label key={item.id} className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={value.includes(item.id)}
              disabled={!value.includes(item.id) && value.length >= 32}
              onChange={(event) => onChange(event.target.checked
                ? [...value, item.id]
                : value.filter((sourceId) => sourceId !== item.id))}
            />
            <span>{item.name}</span>
          </label>
        ))}
        {!query.isLoading && !query.isError && options.length === 0 && (
          <p className="text-xs text-muted-foreground">{t('gadgetDataEmpty')}</p>
        )}
      </div>
      {query.isLoading && <p role="status" className="text-xs text-muted-foreground">{t('gadgetDataLoading')}</p>}
      {query.isError && <p role="alert" className="text-xs text-destructive">{t('gadgetDataLoadFailed')}</p>}
    </fieldset>
  );
}

/** Sentinel for "no source"; Radix Select cannot hold an empty-string value. */
const NONE = '__none__';

/**
 * Single-select picker over active sources of one provider, using the metadata-only
 * gadget-sources API. Reusable by any source-backed renderer through SOURCE_BACKED_RENDERERS.
 */
export function SourcePicker({ provider, value, onChange, id }: {
  provider: string;
  value: string | null;
  onChange: (sourceId: string | null) => void;
  id: string;
}) {
  const t = useTranslations('github');
  const query = useQuery({
    queryKey: [...dashboardKeys.sources, 'picker', provider],
    queryFn: ({ signal }) => listGadgetSources(100, undefined, signal),
  });
  const options = (query.data?.items ?? []).filter(
    (item) => item.provider === provider && (item.status === 'active' || item.id === value),
  );
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id} className="text-xs font-semibold">{t('sourceLabel')}</Label>
      <Select value={value ?? NONE} onValueChange={(next) => onChange(next === NONE ? null : next)}>
        <SelectTrigger id={id} className="h-8 text-xs">
          <SelectValue placeholder={t('sourceNone')} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NONE} className="text-xs">{t('sourceNone')}</SelectItem>
          {options.map((item) => (
            <SelectItem key={item.id} value={item.id} className="text-xs">{item.name}</SelectItem>
          ))}
        </SelectContent>
      </Select>
      {query.isLoading && <p role="status" className="text-xs text-muted-foreground">{t('sourceLoading')}</p>}
      {query.isError && <p role="alert" className="text-xs text-destructive">{t('sourceError')}</p>}
      {!query.isLoading && !query.isError && options.length === 0 && (
        <p className="text-xs text-muted-foreground">{t('sourceNoneAvailable')}</p>
      )}
    </div>
  );
}
