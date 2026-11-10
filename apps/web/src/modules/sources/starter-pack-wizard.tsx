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
import { clampInterval, connectorKeys, createScopedSource, enableBuiltinSource, getConnectorCatalog, intervalBelowMinimum, listBuiltinSources, listSources, sourceKeys, type BuiltinSource, type ConnectorCatalogEntry } from './api';
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

const FIELD_LABEL = { vn_symbols: 'starterFieldVnSymbols', job_keywords: 'starterFieldJobKeywords', crypto_symbols: 'starterFieldCrypto', github_repo: 'starterFieldRepo', arxiv_category: 'starterFieldArxiv', weather: 'starterFieldWeather' } as const;

/** Badge message key per row state; rows without a status yet read as pending. */
const STATUS_KEY = { creating: 'starterStatusCreating', done: 'starterStatusDone', exists: 'starterStatusExists', failed: 'starterStatusFailed' } as const;

/** Everything needed to create one starter item; `name` is already trimmed. */
interface CreateContext {
  csrfToken: string;
  builtinById: Map<string, BuiltinSource>;
  catalog: ConnectorCatalogEntry[];
  name: string;
  text: string;
}

/**
 * Creates one starter item: built-ins use the idempotent enable route (sending a custom name only when the built-in
 * accepts one), scoped items go through `createScopedSource`. Throws whatever the API throws; the caller classifies it.
 */
async function createStarterItem(item: PackItem, { csrfToken, builtinById, catalog, name, text }: CreateContext): Promise<void> {
  if (item.kind === 'builtin') {
    const row = builtinById.get(item.provider);
    const options = builtinOptionsFor(item, text);
    const sendName = name && name !== row?.label && (row?.applicable_options?.includes('name') ?? true);
    await enableBuiltinSource(item.provider, 'personal', csrfToken, undefined, { graph_enabled: false, rag_index_enabled: false, ...options, ...(sendName ? { name } : {}) });
    return;
  }
  const entry = catalog.find((candidate) => candidate.provider_id === item.provider);
  await createScopedSource(scopedSpecFor(item, text, name, entry), csrfToken);
}

/** One selectable starter row: checkbox, editable name, (when not yet added) the in-row value editor with validation, and a note when the provider floor raised the interval (`raisedTo`). */
function StarterItemRow({ item, added, name, checked, text, defaultName, locationError, raisedTo, onToggle, onName, onText, onUseLocation }: {
  item: PackItem; added: boolean; raisedTo: number | null; name: string; checked: boolean; text: string; defaultName: string; locationError: boolean;
  onToggle: (next: boolean) => void; onName: (value: string) => void; onText: (value: string) => void; onUseLocation: () => void;
}) {
  const t = useTranslations('sources');
  const invalid = checked && !editTextValid(item, text);
  return <div className="flex flex-col gap-2 rounded-md border border-border p-3">
    <div className="flex flex-wrap items-center gap-3">
      <Checkbox aria-label={t('starterItemSelect', { name: defaultName })} checked={checked} disabled={added} onCheckedChange={(next) => onToggle(next === true)} />
      <Input aria-label={t('starterNameLabel', { name: defaultName })} aria-invalid={checked && name.trim().length === 0} className="h-8 min-w-0 flex-1 basis-56" maxLength={200} disabled={added} value={name}
        onChange={(event) => onName(event.target.value)} />
      {added && <Badge variant="secondary">{t('starterAlreadyAdded')}</Badge>}
    </div>
    {raisedTo !== null && !added && <small className="text-muted-foreground" role="note">{t('starterIntervalRaised', { minutes: raisedTo })}</small>}
    {item.edit && !added && <div className="field">
      <Label htmlFor={`starter-${item.id}-value`}>{t(FIELD_LABEL[item.edit])}</Label>
      <div className="flex flex-wrap items-center gap-2">
        <Input id={`starter-${item.id}-value`} className="min-w-0 flex-1 basis-56" aria-invalid={invalid} value={text} onChange={(event) => onText(event.target.value)} />
        {item.edit === 'weather' && <Button type="button" variant="outline" size="sm" onClick={onUseLocation}>{t('starterUseMyLocation')}</Button>}
      </div>
      {invalid && <small className="text-destructive" role="alert">{t('starterFieldInvalid')}</small>}
      {item.edit === 'weather' && locationError && <small className="text-destructive" role="alert">{t('starterLocationFailed')}</small>}
    </div>}
  </div>;
}

/** LinkedIn-via-Gmail guide card shown under the jobs pack; offers the generic connector wizard when available. */
function LinkedinGuide({ onAddConnector }: { onAddConnector?: () => void }) {
  const t = useTranslations('sources');
  return <div className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3" data-testid="starter-linkedin-guide">
    <strong>{t('starterLinkedinTitle')}</strong>
    <p className="m-0 text-sm text-muted-foreground">{t('starterLinkedinIntro')}</p>
    <ol className="m-0 pl-5 text-sm"><li>{t('starterLinkedinStep1')}</li><li>{t('starterLinkedinStep2')}</li><li>{t('starterLinkedinStep3')}</li></ol>
    {onAddConnector && <div><Button type="button" variant="outline" size="sm" onClick={onAddConnector}>{t('starterLinkedinAddGmail')}</Button></div>}
  </div>;
}

/** Step 2: running/summary line (plus the optional Build-my-dashboard action once something exists), one status row per frozen item and a retry button for failed rows. */
function StarterProgressStep({ running, runItems, statuses, nameOf, onRetry, onBuildDashboard }: {
  running: boolean; runItems: PackItem[]; statuses: Record<string, RowStatus>; nameOf: (item: PackItem) => string; onRetry: (items: PackItem[]) => void; onBuildDashboard?: () => void;
}) {
  const t = useTranslations('sources');
  const failed = runItems.filter((item) => statuses[item.id]?.state === 'failed');
  const doneCount = runItems.filter((item) => statuses[item.id]?.state === 'done' || statuses[item.id]?.state === 'exists').length;
  return <section className="flex flex-col gap-3" aria-live="polite" aria-label={t('stepStarterCreate')}>
    {running && <p role="status">{t('starterRunning')}</p>}
    {!running && <p role="status">{t('starterSummary', { done: doneCount, failed: failed.length })}</p>}
    <ul className="m-0 flex list-none flex-col gap-2 p-0">
      {runItems.map((item) => {
        const status = statuses[item.id];
        return <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-3" data-testid={`starter-row-${item.id}`}>
          <span className="font-medium">{nameOf(item)}</span>
          <span className="flex items-center gap-2 text-sm">
            {status?.state === 'failed' && status.error && <span className="text-destructive" role="alert">{t(status.error)}</span>}
            <Badge variant={status?.state === 'failed' ? 'outline' : 'secondary'}>{t(status ? STATUS_KEY[status.state] : 'starterStatusPending')}</Badge>
          </span>
        </li>;
      })}
    </ul>
    {!running && onBuildDashboard && doneCount > 0 && <div><Button type="button" onClick={onBuildDashboard}>{t('buildDashboard')}</Button></div>}
    {!running && failed.length > 0 && <div><Button type="button" variant="outline" onClick={() => onRetry(failed)}>{t('starterRetryFailed')}</Button></div>}
  </section>;
}

/** Provider-floor interval when it exceeds the pack's preferred interval, else null (built-ins have no pack interval). */
function raisedInterval(item: PackItem, catalog: ConnectorCatalogEntry[] | undefined): number | null {
  if (item.kind !== 'scoped') return null;
  const entry = catalog?.find((candidate) => candidate.provider_id === item.provider);
  const effective = clampInterval(item.intervalMinutes, entry);
  return effective > item.intervalMinutes ? effective : null;
}

/** Owner edits to the choose step, keyed by item id: unchecked set, custom names and custom edit texts. */
function useStarterEdits() {
  const [unchecked, setUnchecked] = useState<Set<string>>(new Set());
  const [names, setNames] = useState<Record<string, string>>({});
  const [texts, setTexts] = useState<Record<string, string>>({});
  /** Checks or unchecks one item. */
  const toggle = (id: string, on: boolean) => setUnchecked((current) => { const copy = new Set(current); if (on) copy.delete(id); else copy.add(id); return copy; });
  return { unchecked, names, texts, toggle, setName: (id: string, value: string) => setNames((c) => ({ ...c, [id]: value })), setText: (id: string, value: string) => setTexts((c) => ({ ...c, [id]: value })) };
}

/**
 * Two-step starter-pack dialog: choose ready-made sources grouped by pack, then create them one at a time.
 * Everything is preselected except sources the owner already has (built-ins with a source, scoped sources matched by
 * provider and default name). Built-ins the server does not list are hidden. Creation is sequential and continues after a
 * failure; failed rows can be retried without touching rows that succeeded. Built-ins use the idempotent enable route
 * (409 already-added counts as present); scoped sources go through `createScopedSource`, which archives a half-made source.
 * Terms are accepted as personal use for all rows with the single note shown above the list.
 * `onAddConnector` opens the generic connector wizard for the LinkedIn-via-Gmail guide card. `onBuildDashboard`, when given,
 * adds a primary "Build my dashboard" action to the finished run (at least one source created or present).
 */
export function StarterPackWizard({ onClose, onChanged, onAddConnector, onBuildDashboard }: { onClose: () => void; onChanged: () => void; onAddConnector?: () => void; onBuildDashboard?: () => void }) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const builtins = useQuery({ queryKey: connectorKeys.builtins, queryFn: ({ signal }) => listBuiltinSources(signal) });
  const existing = useQuery({ queryKey: sourceKeys.list, queryFn: () => listSources() });
  const catalog = useQuery({ queryKey: connectorKeys.catalog, queryFn: ({ signal }) => getConnectorCatalog(signal), staleTime: 5 * 60_000 });
  const [index, setIndex] = useState(0);
  const edits = useStarterEdits();
  const [statuses, setStatuses] = useState<Record<string, RowStatus>>({});
  // Items frozen when creation starts: the refetched caches mark created items as added, which would otherwise drop their rows.
  const [runItems, setRunItems] = useState<PackItem[]>([]);
  const [running, setRunning] = useState(false);
  const [locationError, setLocationError] = useState(false);

  const builtinById = new Map((builtins.data ?? []).map((row) => [row.id, row]));
  /** Default display name: the server label for built-ins, a message for scoped items. */
  const defaultName = (item: PackItem) => item.kind === 'builtin' ? builtinById.get(item.provider)?.label ?? item.provider : t(item.nameKey);
  const nameOf = (item: PackItem) => edits.names[item.id] ?? defaultName(item);
  const textOf = (item: PackItem) => edits.texts[item.id] ?? defaultEditText(item);
  /** True when the owner already has this item, so it is shown as added and not preselected. */
  const isAdded = (item: PackItem) => item.kind === 'builtin'
    ? builtinById.get(item.provider)?.source_id != null
    : (existing.data?.items ?? []).some((source) => source.provider === item.provider && source.status !== 'archived' && source.name.trim().toLowerCase() === defaultName(item).trim().toLowerCase());
  const loaded = builtins.isSuccess && existing.isSuccess;
  const packs = STARTER_PACKS.map((pack) => ({ ...pack, items: pack.items.filter((item) => item.kind === 'scoped' || builtinById.has(item.provider)) }))
    .filter((pack) => pack.items.length > 0 || pack.guide);
  const picked = packs.flatMap((pack) => pack.items).filter((item) => !isAdded(item) && !edits.unchecked.has(item.id));
  const ready = loaded && picked.length > 0 && picked.every((item) => nameOf(item).trim().length > 0 && editTextValid(item, textOf(item)));

  /** Creates the given items one after another, recording each outcome; one failure never stops the rest. */
  async function run(targets: PackItem[]) {
    setRunning(true);
    try {
      for (const item of targets) {
        setStatuses((current) => ({ ...current, [item.id]: { state: 'creating' } }));
        try {
          await createStarterItem(item, { csrfToken, builtinById, catalog: catalog.data ?? [], name: nameOf(item).trim(), text: textOf(item) });
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
      (position) => edits.setText(item.id, `${position.coords.latitude.toFixed(4)}, ${position.coords.longitude.toFixed(4)}`),
      () => setLocationError(true),
    );
  }

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
          const raised = raisedInterval(item, catalog.data);
          return <StarterItemRow key={item.id} item={item} added={added} name={nameOf(item)} checked={!added && !edits.unchecked.has(item.id)} text={textOf(item)}
            defaultName={defaultName(item)} locationError={locationError} raisedTo={raised} onToggle={(on) => edits.toggle(item.id, on)}
            onName={(value) => edits.setName(item.id, value)} onText={(value) => edits.setText(item.id, value)} onUseLocation={() => fillMyLocation(item)} />;
        })}
        {pack.guide === 'linkedin' && <LinkedinGuide onAddConnector={onAddConnector} />}
      </div>)}
      {loaded && !ready && picked.length === 0 && <p className="m-0 text-sm text-muted-foreground" role="status">{t('starterNothing')}</p>}
    </section>}
    {index === 1 && <StarterProgressStep running={running} runItems={runItems} statuses={statuses} nameOf={nameOf} onRetry={(items) => void run(items)} onBuildDashboard={onBuildDashboard} />}
  </WizardShell>;
}
