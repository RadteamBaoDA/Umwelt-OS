'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { connectorKeys, createScopedSource, enableBuiltinSource, getConnectorCatalog, intervalBelowMinimum, listBuiltinSources, listSources, sourceKeys } from './api';
import { builtinEnableErrorKey } from './builtin-sources';
import { WizardShell } from './source-wizard';
import { builtinOptionsFor, defaultEditText, editTextValid, scopedSpecFor, STARTER_PACKS, type PackItem } from './starter-packs';

/** Per-row creation state; `error` is a `sources` message key shown for failed rows. */
type RowStatus = { state: 'creating' | 'done' | 'exists' | 'failed'; error?: 'builtinErrorOperator' | 'builtinErrorTerms' | 'builtinErrorGeneric' | 'scheduleBelowMinimum' };

/** Maps a failed create to a translated message key; an already-added built-in is not a failure. */
function failureKey(cause: unknown): 'exists' | NonNullable<RowStatus['error']> {
  if (intervalBelowMinimum(cause)) return 'scheduleBelowMinimum';
  const key = builtinEnableErrorKey(cause);
  return key === 'builtinAlreadyAdded' ? 'exists' : key;
}

/**
 * Two-step starter-pack dialog: choose ready-made sources grouped by pack, then create them one at a time.
 * Everything is preselected except sources the owner already has (built-ins with a source, scoped sources matched by
 * provider and default name). Built-ins the server does not list are hidden. Creation is sequential and continues after a
 * failure; failed rows can be retried without touching rows that succeeded. Built-ins use the idempotent enable route
 * (409 already-added counts as present); scoped sources go through `createScopedSource`, which archives a half-made source.
 * Terms are accepted as personal use for all rows with the single note shown above the list.
 * `onAddConnector` opens the generic connector wizard for the LinkedIn-via-Gmail guide card.
 */
export function StarterPackWizard({ onClose, onChanged, onAddConnector }: { onClose: () => void; onChanged: () => void; onAddConnector?: () => void }) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const builtins = useQuery({ queryKey: connectorKeys.builtins, queryFn: ({ signal }) => listBuiltinSources(signal) });
  const existing = useQuery({ queryKey: sourceKeys.list, queryFn: () => listSources() });
  const catalog = useQuery({ queryKey: connectorKeys.catalog, queryFn: ({ signal }) => getConnectorCatalog(signal), staleTime: 5 * 60_000 });
  const [index, setIndex] = useState(0);
  const [unchecked, setUnchecked] = useState<Set<string>>(new Set());
  const [names, setNames] = useState<Record<string, string>>({});
  const [texts, setTexts] = useState<Record<string, string>>({});
  const [statuses, setStatuses] = useState<Record<string, RowStatus>>({});
  // Items frozen when creation starts: the refetched caches mark created items as added, which would otherwise drop their rows.
  const [runItems, setRunItems] = useState<PackItem[]>([]);
  const [running, setRunning] = useState(false);
  const [locationError, setLocationError] = useState(false);

  const builtinById = new Map((builtins.data ?? []).map((row) => [row.id, row]));
  /** Default display name: the server label for built-ins, a message for scoped items. */
  const defaultName = (item: PackItem) => item.kind === 'builtin' ? builtinById.get(item.provider)?.label ?? item.provider : t(item.nameKey);
  const nameOf = (item: PackItem) => names[item.id] ?? defaultName(item);
  const textOf = (item: PackItem) => texts[item.id] ?? defaultEditText(item);
  /** True when the owner already has this item, so it is shown as added and not preselected. */
  const isAdded = (item: PackItem) => item.kind === 'builtin'
    ? builtinById.get(item.provider)?.source_id != null
    : (existing.data?.items ?? []).some((source) => source.provider === item.provider && source.status !== 'archived' && source.name.trim().toLowerCase() === defaultName(item).trim().toLowerCase());
  const loaded = builtins.isSuccess && existing.isSuccess;
  const packs = STARTER_PACKS.map((pack) => ({ ...pack, items: pack.items.filter((item) => item.kind === 'scoped' || builtinById.has(item.provider)) }))
    .filter((pack) => pack.items.length > 0 || pack.guide);
  const allItems = packs.flatMap((pack) => pack.items);
  const picked = allItems.filter((item) => !isAdded(item) && !unchecked.has(item.id));
  const ready = loaded && picked.length > 0 && picked.every((item) => nameOf(item).trim().length > 0 && editTextValid(item, textOf(item)));

  /** Creates the given items one after another, recording each outcome; one failure never stops the rest. */
  async function run(targets: PackItem[]) {
    setRunning(true);
    try {
      for (const item of targets) {
        setStatuses((current) => ({ ...current, [item.id]: { state: 'creating' } }));
        try {
          const name = nameOf(item).trim();
          if (item.kind === 'builtin') {
            const row = builtinById.get(item.provider);
            const options = builtinOptionsFor(item, textOf(item));
            const sendName = name && name !== row?.label && (row?.applicable_options?.includes('name') ?? true);
            await enableBuiltinSource(item.provider, 'personal', csrfToken, undefined, { graph_enabled: false, rag_index_enabled: false, ...options, ...(sendName ? { name } : {}) });
          } else {
            const entry = (catalog.data ?? []).find((candidate) => candidate.provider_id === item.provider);
            await createScopedSource(scopedSpecFor(item, textOf(item), name, entry), csrfToken);
          }
          setStatuses((current) => ({ ...current, [item.id]: { state: 'done' } }));
        } catch (cause) {
          const key = failureKey(cause);
          setStatuses((current) => ({ ...current, [item.id]: key === 'exists' ? { state: 'exists' } : { state: 'failed', error: key } }));
        }
      }
    } finally {
      setRunning(false);
      void queryClient.invalidateQueries({ queryKey: connectorKeys.builtins });
      void queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      onChanged();
    }
  }

  /** Starts creation when leaving the choose step; the progress step shows the outcome. */
  async function onNext(): Promise<boolean> {
    if (index !== 0) return true;
    setRunItems(picked);
    void run(picked);
    return true;
  }

  /** Fills the weather row with the device position (replaces the Ho Chi Minh City default). */
  function fillMyLocation(item: PackItem) {
    setLocationError(false);
    if (!navigator.geolocation) { setLocationError(true); return; }
    navigator.geolocation.getCurrentPosition(
      (position) => setTexts((current) => ({ ...current, [item.id]: `${position.coords.latitude.toFixed(4)}, ${position.coords.longitude.toFixed(4)}` })),
      () => setLocationError(true),
    );
  }

  const failed = runItems.filter((item) => statuses[item.id]?.state === 'failed');
  const doneCount = runItems.filter((item) => statuses[item.id]?.state === 'done' || statuses[item.id]?.state === 'exists').length;
  const fieldLabel = { vn_symbols: 'starterFieldVnSymbols', job_keywords: 'starterFieldJobKeywords', crypto_symbols: 'starterFieldCrypto', github_repo: 'starterFieldRepo', arxiv_category: 'starterFieldArxiv', weather: 'starterFieldWeather' } as const;
  const steps = [
    { id: 'choose', title: t('stepStarterChoose'), valid: ready },
    { id: 'create', title: t('stepStarterCreate'), valid: true },
  ];

  return <WizardShell title={t('starterTitle')} mode="add" steps={steps} stepIndex={index} onStepChange={setIndex} busy={running} onNext={onNext} onRequestClose={onClose}
    final={{ label: t('done'), disabled: running, onClick: onClose }}>
    {index === 0 && <section className="flex flex-col gap-4" aria-label={t('stepStarterChoose')}>
      <p className="m-0 text-muted-foreground">{t('starterIntro')}</p>
      <p className="m-0 text-sm text-muted-foreground" role="note">{t('starterTermsNote')}</p>
      {(builtins.isPending || existing.isPending) && <p className="text-muted-foreground">{t('loading')}</p>}
      {(builtins.isError || existing.isError) && <p className="text-destructive" role="alert">{t('starterLoadFailed')} <Button variant="outline" onClick={() => { void builtins.refetch(); void existing.refetch(); }}>{t('retry')}</Button></p>}
      {loaded && packs.map((pack) => <div key={pack.id} className="flex flex-col gap-2" role="group" aria-label={t(pack.labelKey)}>
        <h3 className="m-0 text-sm font-semibold text-muted-foreground">{t(pack.labelKey)}</h3>
        {pack.items.map((item) => {
          const added = isAdded(item);
          const name = nameOf(item);
          const checked = !added && !unchecked.has(item.id);
          const text = textOf(item);
          return <div key={item.id} className="flex flex-col gap-2 rounded-md border border-border p-3">
            <div className="flex flex-wrap items-center gap-3">
              <Checkbox aria-label={t('starterItemSelect', { name: defaultName(item) })} checked={checked} disabled={added}
                onCheckedChange={(next) => setUnchecked((current) => { const copy = new Set(current); if (next === true) copy.delete(item.id); else copy.add(item.id); return copy; })} />
              <Input aria-label={t('starterNameLabel', { name: defaultName(item) })} aria-invalid={checked && name.trim().length === 0} className="h-8 min-w-0 flex-1 basis-56" maxLength={200} disabled={added} value={name}
                onChange={(event) => setNames((current) => ({ ...current, [item.id]: event.target.value }))} />
              {added && <Badge variant="secondary">{t('starterAlreadyAdded')}</Badge>}
            </div>
            {item.edit && !added && <div className="field">
              <Label htmlFor={`starter-${item.id}-value`}>{t(fieldLabel[item.edit])}</Label>
              <div className="flex flex-wrap items-center gap-2">
                <Input id={`starter-${item.id}-value`} className="min-w-0 flex-1 basis-56" aria-invalid={checked && !editTextValid(item, text)} value={text}
                  onChange={(event) => setTexts((current) => ({ ...current, [item.id]: event.target.value }))} />
                {item.edit === 'weather' && <Button type="button" variant="outline" size="sm" onClick={() => fillMyLocation(item)}>{t('starterUseMyLocation')}</Button>}
              </div>
              {checked && !editTextValid(item, text) && <small className="text-destructive" role="alert">{t('starterFieldInvalid')}</small>}
              {item.edit === 'weather' && locationError && <small className="text-destructive" role="alert">{t('starterLocationFailed')}</small>}
            </div>}
          </div>;
        })}
        {pack.guide === 'linkedin' && <div className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3" data-testid="starter-linkedin-guide">
          <strong>{t('starterLinkedinTitle')}</strong>
          <p className="m-0 text-sm text-muted-foreground">{t('starterLinkedinIntro')}</p>
          <ol className="m-0 pl-5 text-sm"><li>{t('starterLinkedinStep1')}</li><li>{t('starterLinkedinStep2')}</li><li>{t('starterLinkedinStep3')}</li></ol>
          {onAddConnector && <div><Button type="button" variant="outline" size="sm" onClick={onAddConnector}>{t('starterLinkedinAddGmail')}</Button></div>}
        </div>}
      </div>)}
      {loaded && !ready && picked.length === 0 && <p className="m-0 text-sm text-muted-foreground" role="status">{t('starterNothing')}</p>}
    </section>}
    {index === 1 && <section className="flex flex-col gap-3" aria-live="polite" aria-label={t('stepStarterCreate')}>
      {running && <p role="status">{t('starterRunning')}</p>}
      {!running && <p role="status">{t('starterSummary', { done: doneCount, failed: failed.length })}</p>}
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {runItems.map((item) => {
          const status = statuses[item.id];
          return <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-3" data-testid={`starter-row-${item.id}`}>
            <span className="font-medium">{nameOf(item)}</span>
            <span className="flex items-center gap-2 text-sm">
              {status?.state === 'failed' && status.error && <span className="text-destructive" role="alert">{t(status.error)}</span>}
              <Badge variant={status?.state === 'failed' ? 'outline' : 'secondary'}>{t(status?.state === 'creating' ? 'starterStatusCreating' : status?.state === 'done' ? 'starterStatusDone' : status?.state === 'exists' ? 'starterStatusExists' : status?.state === 'failed' ? 'starterStatusFailed' : 'starterStatusPending')}</Badge>
            </span>
          </li>;
        })}
      </ul>
      {!running && failed.length > 0 && <div><Button type="button" variant="outline" onClick={() => void run(failed)}>{t('starterRetryFailed')}</Button></div>}
    </section>}
  </WizardShell>;
}
