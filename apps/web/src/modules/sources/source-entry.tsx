'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useErrorCodeLabel } from '@/core/error-codes';
import { useEffect, useRef, useState } from 'react';
import { Download, History, Pause, Play, RefreshCw, Settings2, Trash2, Unplug } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useDisplayPreferences } from '@/core/query-provider';
import { AppLocaleId, formatTimeZoneLabel } from '@/core/i18n';
import { formatDateTime } from '@/core/format';
import {
  connectorKeys,
  getConnectorCatalog,
  getConnectorActivation,
  getOperation,
  collectionTooSoonRetryAt,
  RealtimeMode,
  Source,
  sourceKeys,
  triggerCollection,
  type ConnectorActivation,
} from './api';
import { DisconnectDialog } from './disconnect-dialog';
import { useSourceActions } from './use-source-actions';
import { RenameSource } from './source-rename';
import { CompatibleChips } from './compatible-chips';
import { SourceRowState, sourceRowState } from './source-table';
import { SyncHistory } from './sync-history';

/** Renders an accessible icon-only source action with the same translated name in its tooltip and accessible label. */
function SourceAction({
  label,
  icon,
  onClick,
  disabled = false,
  expanded,
}: {
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  expanded?: boolean;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button type="button" size="icon" variant="ghost" aria-label={label} aria-expanded={expanded} disabled={disabled} onClick={onClick}>
          {icon}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

/** Formats an optional ISO timestamp with the supplied application locale and time zone, returning the supplied fallback when absent. */
function formatDate(value: string | null, locale: AppLocaleId, timezone: string, fallback: string): string {
  return value ? formatDateTime(value, locale, timezone) : fallback;
}

/** Resolves registered provider IDs to translated names, then to the catalog label, so new providers need no message key; otherwise the raw ID. */
function providerLabel(provider: string | null, t: ReturnType<typeof useTranslations<'sources'>>, catalogLabel?: string) {
  if (!provider) return null;
  const keys: Record<string, string> = {
    youtube: 'providerYoutube',
    arxiv: 'providerArxiv',
    huggingface: 'providerHuggingface',
    github_releases: 'providerGithubReleases',
    telegram: 'providerTelegram',
    vn_stock: 'providerVnStock',
    github_trending: 'providerGithubTrending',
    wwr_jobs: 'providerWwrJobs',
    vietnamworks_jobs: 'providerVietnamworksJobs',
    itviec_jobs: 'providerItviecJobs',
    rss: 'providerRss',
    web: 'providerWeb',
    rest: 'providerRest',
  };
  const key = keys[provider];
  return key ? t(key as 'providerYoutube') : (catalogLabel ?? provider);
}

/** Formats a connector schedule interval as localized display text. */
export function scheduleLabel(minutes: number, t: ReturnType<typeof useTranslations<'sources'>>) {
  if (minutes === 15 || minutes === 30) return t('everyMinutes', { minutes });
  if (minutes === 60) return t('everyHour');
  if (minutes === 360) return t('everyHours', { hours: 6 });
  if (minutes === 1440) return t('dailyAtMidnight');
  return t('scheduleUnknown');
}

/** Shows allowlisted owner cleanup progress, polling for at most 30 seconds with abortable GETs and manual refresh afterward. */
function PurgeProgress({ operationId, onSucceeded }: { operationId: string; onSucceeded: () => void }) {
  const t = useTranslations('sources');
  const pollingStartedAt = useRef<number | null>(null);
  const operation = useQuery({
    queryKey: ['operation', operationId],
    queryFn: ({ signal }) => {
      pollingStartedAt.current ??= Date.now();
      const controller = new AbortController();
      const timeoutId = window.setTimeout(() => controller.abort(), 10_000);
      const cancelQuery = () => controller.abort(signal.reason);
      if (signal.aborted) cancelQuery();
      else signal.addEventListener('abort', cancelQuery, { once: true });
      return getOperation(operationId, controller.signal)
        .then((result) => {
          return result;
        })
        .finally(() => {
          window.clearTimeout(timeoutId);
          signal.removeEventListener('abort', cancelQuery);
        });
    },
    refetchInterval: (query) => {
      const current = query.state.data;
      const terminal =
        current?.status === 'succeeded' ||
        current?.status === 'failed' ||
        current?.documents_status === 'failed' ||
        current?.documents_status === 'unavailable';
      return terminal || Date.now() - (pollingStartedAt.current ?? Date.now()) >= 30_000 ? false : 1500;
    },
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });
  const succeeded = operation.data?.status === 'succeeded';
  useEffect(() => {
    if (succeeded) onSucceeded();
  }, [succeeded, onSucceeded]);
  const refreshButton = (
    <Button type="button" variant="outline" disabled={operation.isFetching} onClick={() => void operation.refetch()}>
      {operation.isFetching ? t('cleanupRefreshingProgress') : t('cleanupRefreshProgress')}
    </Button>
  );
  if (!operation.data)
    return (
      <div>
        <p className={operation.isError ? 'error' : 'muted'} role={operation.isError ? 'alert' : 'status'}>
          {operation.isError ? t('actionFailed') : t('loading')}
        </p>
        {refreshButton}
      </div>
    );

  const ownerLabels = new Map<string, string>([
    ['documents', t('cleanupOwnerDocuments')],
    ['raw', t('cleanupOwnerRaw')],
    ['chat', t('cleanupOwnerChat')],
    ['memory', t('cleanupOwnerMemory')],
    ['agents', t('cleanupOwnerAgents')],
    ['dashboard', t('cleanupOwnerDashboard')],
    ['notifications', t('cleanupOwnerNotifications')],
    ['automations', t('cleanupOwnerAutomations')],
  ]);
  const pendingOwners = [
    ...new Set(operation.data.pending_owner_codes.map((code) => ownerLabels.get(code) ?? t('cleanupOwnerOther'))),
  ].slice(0, 9);
  return (
    <div>
      <p role="status">
        {succeeded ? t('cleanupComplete') : operation.data.status}
        {operation.data.error_code ? ` · ${operation.data.error_code}` : ''}
      </p>
      {operation.data.memory_status === 'failed' &&
        (operation.data.memory_error_code === 'legacy_provenance_unresolved' ||
          operation.data.memory_error_code === 'evidence_identity_unavailable') && (
          <p className="error" role="alert">
            {t('cleanupMemoryUnavailable')}
          </p>
        )}
      {pendingOwners.length > 0 && (
        <p className="text-muted-foreground">{t('cleanupPendingOwnerStages', { owners: pendingOwners.join(', ') })}</p>
      )}
      {refreshButton}
    </div>
  );
}

const CHIP_KEY: Record<
  SourceRowState,
  'statusActive' | 'statusPaused' | 'statusArchived' | 'stateSavedNotActive' | 'stateError' | 'stateReconnect'
> = {
  active: 'statusActive',
  paused: 'statusPaused',
  archived: 'statusArchived',
  savedNotActive: 'stateSavedNotActive',
  error: 'stateError',
  reconnect: 'stateReconnect',
};
const ACTIVATION_STATE_KEY: Record<string, string> = {
  queued: 'stateQueued',
  provisioning: 'stateProvisioning',
  active: 'active',
  saved_not_active: 'stateSavedNotActive',
  reconciliation_required: 'stateReconciliationRequired',
  disabled: 'stateDisabled',
};
const CELL = 'px-3 py-3 align-top text-sm max-[1023px]:block max-[1023px]:px-0 max-[1023px]:py-1';

/** Border/text classes of the status chip: green when active, red for error and saved-but-not-active, neutral otherwise. */
function chipClass(rowState: SourceRowState): string | undefined {
  if (rowState === 'active') return 'border-positive text-positive';
  return rowState === 'error' || rowState === 'savedNotActive' ? 'border-destructive text-destructive' : undefined;
}

/**
 * "Collect now" mutation for one source. A 429 `collection_too_soon` names the next allowed time and blocks the action
 * until then (or 60 s when no time is given); success records the run outcome and refreshes the source's history/detail.
 */
function useCollectNow(sourceId: string, csrfToken: string) {
  const queryClient = useQueryClient();
  const [collectResult, setCollectResult] = useState<{ status: string; hasRun: boolean } | null>(null);
  const [collectNextAt, setCollectNextAt] = useState<Date | null>(null);
  const [collectBlocked, setCollectBlocked] = useState(false);
  useEffect(() => {
    if (!collectBlocked) return;
    const wait = collectNextAt ? Math.max(collectNextAt.getTime() - Date.now(), 0) : 60_000;
    const timer = window.setTimeout(() => setCollectBlocked(false), wait);
    return () => window.clearTimeout(timer);
  }, [collectBlocked, collectNextAt]);
  const collect = useMutation({
    mutationFn: () => triggerCollection(sourceId, csrfToken),
    onError: (cause) => {
      const retryAt = collectionTooSoonRetryAt(cause);
      if (retryAt === undefined) return;
      setCollectNextAt(retryAt);
      setCollectBlocked(true);
    },
    onSuccess: (result) => {
      setCollectResult({ status: result.status, hasRun: Boolean(result.run_id) });
      void queryClient.invalidateQueries({ queryKey: connectorKeys.ingestion(sourceId) });
      void queryClient.invalidateQueries({ queryKey: sourceKeys.detail(sourceId) });
    },
  });
  return { collect, collectResult, collectNextAt, collectBlocked };
}

/**
 * Focus hand-off for a row removed by a confirmed disconnect. The name cell is focused after the confirm dialog closes;
 * if the row then unmounts and focus fell to <body>, `onRemoved` hands it to the list heading.
 */
function useRemovedFocusHandoff(onRemoved: () => void) {
  const nameCellRef = useRef<HTMLTableCellElement>(null);
  const confirmedRef = useRef(false);
  const everConfirmedRef = useRef(false);
  const onRemovedRef = useRef(onRemoved);
  useEffect(() => {
    onRemovedRef.current = onRemoved;
  });
  // A confirmed disconnect that unmounts the row leaves focus on <body>; hand it to the list heading.
  useEffect(
    () => () => {
      const lost = !document.activeElement || document.activeElement === document.body;
      if (everConfirmedRef.current && lost) onRemovedRef.current();
    },
    [],
  );
  /** Records that the owner confirmed a disconnect, so the next dialog close moves focus to the name cell. */
  const markConfirmed = () => {
    confirmedRef.current = true;
    everConfirmedRef.current = true;
  };
  /** After a confirmed disconnect, keeps focus off the vanishing trigger by focusing the name cell instead. */
  const restoreFocus = (event: Event) => {
    if (!confirmedRef.current) return;
    confirmedRef.current = false;
    event.preventDefault();
    nameCellRef.current?.focus();
  };
  return { nameCellRef, markConfirmed, restoreFocus };
}

/** Localised reasons for the blocked codes the backend emits; other codes fall back to the humanized code. */
const BLOCKED_REASON_KEY = {
  invalid_credential: 'blockedInvalidCredential',
  schema_changed: 'blockedSchemaChanged',
  terms_not_accepted: 'blockedTermsNotAccepted',
  credential_missing: 'blockedCredentialMissing',
} as const;

/** Why a source stopped (`blocked_error_code`) and how many attempts failed; renders nothing for a healthy source. */
export function SourceBlockedNotice({ source }: { source: Pick<Source, 'blocked_error_code' | 'failure_count'> }) {
  const t = useTranslations('sources');
  const errorLabel = useErrorCodeLabel();
  const code = source.blocked_error_code;
  const failures = source.failure_count ?? 0;
  if (!code && failures <= 0) return null;
  const known = code ? BLOCKED_REASON_KEY[code as keyof typeof BLOCKED_REASON_KEY] : undefined;
  return (
    <p className="m-0 text-sm text-negative">
      {code && t('blockedLine', { reason: known ? t(known) : errorLabel(code) })}
      {code && failures > 0 && ' · '}
      {failures > 0 && t('failureCount', { count: failures })}
    </p>
  );
}

/** Name cell: rename control, realtime badge, provider label, compatible-gadget chips and the latest collection/processing error line. */
function SourceNameCell({
  source,
  cellRef,
  mode,
  catalogLabel,
  onChanged,
}: {
  source: Source;
  cellRef: React.RefObject<HTMLTableCellElement | null>;
  mode: RealtimeMode | null;
  catalogLabel?: string;
  onChanged: () => void;
}) {
  const t = useTranslations('sources');
  const errorLabel = useErrorCodeLabel();
  return (
    <th
      scope="row"
      ref={cellRef}
      tabIndex={-1}
      className={`${CELL} font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <RenameSource source={source} onRenamed={onChanged} />
        {mode && (
          <Badge variant="outline" data-testid="source-realtime-badge">
            {t(mode === 'realtime' ? 'badgeRealtime' : 'badgeNearRealtime')}
          </Badge>
        )}
      </div>
      <p className="m-0 text-sm text-muted-foreground">{providerLabel(source.provider, t, catalogLabel) ?? source.type}</p>
      {source.status === 'active' && <CompatibleChips source={source} />}
      <SourceBlockedNotice source={source} />
      {(source.collection_error_code || source.processing_error_code) && (
        <p className="m-0 text-sm text-negative">
          {source.collection_error_code
            ? t('errorLine', { kind: t('collectionError'), reason: errorLabel(source.collection_error_code) })
            : t('errorLine', { kind: t('processingError'), reason: errorLabel(source.processing_error_code ?? '') })}
        </p>
      )}
    </th>
  );
}

/** Status cell: the row-state chip plus, for connectors, the activation error and non-active activation state. */
function SourceStatusCell({
  rowState,
  connector,
  activation,
}: {
  rowState: SourceRowState;
  connector: boolean;
  activation: ConnectorActivation | undefined;
}) {
  const t = useTranslations('sources');
  const errorLabel = useErrorCodeLabel();
  return (
    <td className={CELL}>
      <Badge variant="outline" className={chipClass(rowState)}>
        {t(CHIP_KEY[rowState])}
      </Badge>
      {connector && activation?.error_code && <p className="text-muted-foreground">{errorLabel(activation.error_code)}</p>}
      {connector && activation && activation.state !== 'active' && (
        <p className="text-muted-foreground">
          {t('activationState')}: {t((ACTIVATION_STATE_KEY[activation.state] ?? 'stateUnknown') as 'stateUnknown')}
        </p>
      )}
    </td>
  );
}

/** Schedule, collected and indexed cells; the mobile layout prefixes each value with its column label. */
function SourceTimingCells({
  source,
  connector,
  schedule,
  timezone,
}: {
  source: Source;
  connector: boolean;
  schedule: string;
  timezone: string | null;
}) {
  const t = useTranslations('sources');
  const display = useDisplayPreferences();
  /** Shows a quiet dash for unknown times while keeping "Not recorded" available to screen readers. */
  const dateOrDash = (value: string | null) =>
    value ? (
      formatDate(value, display.locale, display.timezone, '')
    ) : (
      <>
        <span aria-hidden="true" className="text-muted-foreground">
          —
        </span>
        <span className="sr-only">{t('never')}</span>
      </>
    );
  const mobileLabel = (key: 'colSchedule' | 'colCollected' | 'colIndexed') => (
    <span className="text-muted-foreground mr-1 min-[1024px]:hidden">{t(key)}:</span>
  );
  return (
    <>
      <td className={CELL}>
        {mobileLabel('colSchedule')}
        {connector ? (
          <>
            {schedule}
            {timezone && (
              <span className="whitespace-nowrap">
                {' · '}
                {formatTimeZoneLabel(timezone, display.locale)}
              </span>
            )}
          </>
        ) : (
          '—'
        )}
      </td>
      <td className={`${CELL} tabular-nums min-[1024px]:whitespace-nowrap`}>
        {mobileLabel('colCollected')}
        {dateOrDash(source.collected_at)}
      </td>
      <td className={`${CELL} tabular-nums min-[1024px]:whitespace-nowrap`}>
        {mobileLabel('colIndexed')}
        {dateOrDash(source.indexed_at)}
      </td>
    </>
  );
}

type RowActions = ReturnType<typeof useSourceActions>;
type CollectState = ReturnType<typeof useCollectNow>;
type FocusHandoff = ReturnType<typeof useRemovedFocusHandoff>;

/** Disconnect (or delete, once archived) action with its confirmation dialog and post-confirm focus hand-off. */
function DisconnectAction({
  source,
  busy,
  disconnect,
  handoff,
}: {
  source: Source;
  busy: boolean;
  disconnect: RowActions['disconnect'];
  handoff: FocusHandoff;
}) {
  const t = useTranslations('sources');
  const archived = source.status === 'archived';
  const disconnectLabel = busy ? t('disconnecting') : t(archived ? 'disconnectDelete' : 'disconnectAction');
  return (
    <DisconnectDialog
      sourceId={source.id}
      name={source.name}
      archived={archived}
      disabled={busy}
      triggerLabel={disconnectLabel}
      onConfirm={(deleteData) => {
        handoff.markConfirmed();
        void disconnect(deleteData);
      }}
      onCloseAutoFocus={handoff.restoreFocus}
      trigger={
        <Button type="button" size="icon" variant="ghost" aria-label={disconnectLabel} disabled={busy}>
          <span aria-hidden="true">{archived ? <Trash2 size={16} /> : <Unplug size={16} />}</span>
        </Button>
      }
    />
  );
}

/** Pause/resume toggle whose label reflects the pending transition. */
function PauseResumeAction({ source, busy, onToggle }: { source: Source; busy: boolean; onToggle: () => void }) {
  const t = useTranslations('sources');
  const paused = source.status === 'paused';
  return (
    <SourceAction
      label={busy ? t(paused ? 'resuming' : 'pausing') : t(paused ? 'resume' : 'pause')}
      icon={paused ? <Play size={16} /> : <Pause size={16} />}
      disabled={busy}
      onClick={onToggle}
    />
  );
}

/** Connector-only leading actions: retry activation (saved but not active), configure and collect now (active and activated). */
function ConnectorLeadingActions({
  source,
  rowState,
  activationActive,
  busy,
  collect,
  onEdit,
}: {
  source: Source;
  rowState: SourceRowState;
  activationActive: boolean;
  busy: boolean;
  collect: CollectState;
  onEdit: (source: Source) => void;
}) {
  const t = useTranslations('sources');
  const archived = source.status === 'archived';
  const collecting = collect.collect.isPending;
  return (
    <>
      {rowState === 'savedNotActive' && !archived && (
        <SourceAction label={t('retryActivation')} icon={<RefreshCw size={16} />} disabled={busy} onClick={() => onEdit(source)} />
      )}
      {!archived && <SourceAction label={t('configure')} icon={<Settings2 size={16} />} onClick={() => onEdit(source)} />}
      {source.status === 'active' && activationActive && (
        <SourceAction
          label={collecting ? t('collecting') : t('collectNow')}
          icon={<Download size={16} />}
          disabled={collecting || busy || collect.collectBlocked}
          onClick={() => collect.collect.mutate()}
        />
      )}
    </>
  );
}

/** Icon-only action group: retry activation, configure, collect now, pause/resume, disconnect and history. */
function SourceActionsCell({
  source,
  connector,
  rowState,
  activationActive,
  editedHere,
  purged,
  actions,
  collect,
  handoff,
  onEdit,
  onOpenHistory,
  historyOpen,
}: {
  source: Source;
  connector: boolean;
  rowState: SourceRowState;
  activationActive: boolean;
  editedHere: boolean;
  purged: boolean;
  actions: RowActions;
  collect: CollectState;
  handoff: FocusHandoff;
  onEdit: (source: Source) => void;
  onOpenHistory: () => void;
  historyOpen: boolean;
}) {
  const t = useTranslations('sources');
  const { busy, toggleStatus, disconnect } = actions;
  const archived = source.status === 'archived';
  return (
    <td className={CELL}>
      <TooltipProvider delayDuration={300}>
        <div className="flex flex-nowrap gap-1" role="group" aria-label={t('sourceActions', { name: source.name })}>
          {connector && (
            <ConnectorLeadingActions
              source={source}
              rowState={rowState}
              activationActive={activationActive}
              busy={busy}
              collect={collect}
              onEdit={onEdit}
            />
          )}
          {!editedHere && !archived && <PauseResumeAction source={source} busy={busy} onToggle={toggleStatus} />}
          {!editedHere && !purged && <DisconnectAction source={source} busy={busy} disconnect={disconnect} handoff={handoff} />}
          {connector && (
            <SourceAction label={t('showHistory')} icon={<History size={16} />} expanded={historyOpen} onClick={onOpenHistory} />
          )}
        </div>
      </TooltipProvider>
    </td>
  );
}

/** Detail row under the source: collect result, purge progress, too-soon notice and the generic failure line. */
function SourceDetailRow({
  operationId,
  onPurgeFinished,
  collect,
  error,
  activationError,
}: {
  operationId: string;
  onPurgeFinished: () => void;
  collect: CollectState;
  error: unknown;
  activationError: boolean;
}) {
  const t = useTranslations('sources');
  const display = useDisplayPreferences();
  const { collectResult, collectBlocked, collectNextAt } = collect;
  return (
    <tr className="max-[1023px]:block">
      <td colSpan={6} className="px-3 pb-4 max-[1023px]:block max-[1023px]:px-0">
        {collectResult && (
          <p className="text-muted-foreground" role="status">
            {t('collectResult', { status: collectResult.status })}
            {!collectResult.hasRun ? ` ${t('noRunId')}` : ''}
          </p>
        )}
        {operationId && <PurgeProgress operationId={operationId} onSucceeded={onPurgeFinished} />}
        {collectBlocked && (
          <p className="text-muted-foreground" role="status">
            {collectNextAt
              ? t('collectTooSoonAt', { time: formatDate(collectNextAt.toISOString(), display.locale, display.timezone, '') })
              : t('collectTooSoon')}
          </p>
        )}
        {(Boolean(error) || (collect.collect.error && !collectBlocked) || activationError) && (
          <p className="error" role="alert">
            {t('actionFailed')}
            {activationError && ` · ${t('stateUnknown')}`}
          </p>
        )}
      </td>
    </tr>
  );
}

/** Run-history dialog for one connector source. */
function SourceHistoryDialog({ source, open, onOpenChange }: { source: Source; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations('sources');
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[80vw] max-w-[80vw] sm:max-w-[80vw]">
        <DialogHeader>
          <DialogTitle>
            {t('runHistoryTitle')} · {source.name}
          </DialogTitle>
          <DialogDescription>{t('recentRuns')}</DialogDescription>
        </DialogHeader>
        <SyncHistory source={source} />
      </DialogContent>
    </Dialog>
  );
}

/** Props of one source table row; purge state is lifted into the list so in-flight cleanup rows stay mounted. */
export interface SourceEntryProps {
  source: Source;
  schedule: string;
  timezone: string | null;
  realtimeMode?: RealtimeMode | null;
  operationId: string;
  onPurgeStarted: (operationId: string) => void;
  onPurgeFinished: () => void;
  /** Cleanup finished; the row must not offer Disconnect & delete again. */
  purged: boolean;
  /** Source open in the editor; its Pause/Disconnect live in the editor header only. */
  editingId: string | null;
  onEdit: (source: Source) => void;
  onChanged: () => void;
  /** Called when a confirmed disconnect removes this row, so the owner can place focus. */
  onRemoved: () => void;
}

/** Renders a source’s schedule, status, realtime/near-realtime tracking badge, and actions, including archive or purge confirmation. */
export function SourceEntry({
  source,
  schedule,
  timezone,
  realtimeMode,
  operationId,
  onPurgeStarted,
  onPurgeFinished,
  purged,
  editingId,
  onEdit,
  onChanged,
  onRemoved,
}: SourceEntryProps) {
  const { csrfToken } = useWorkspaceSession();
  const [historyOpen, setHistoryOpen] = useState(false);
  const connector = ['rss', 'web', 'api', 'mcp'].includes(source.type);
  const activation = useQuery({
    queryKey: connectorKeys.activation(source.id),
    queryFn: () => getConnectorActivation(source.id),
    enabled: connector,
  });
  // Shared cache with the editor; supplies the label and default tier for providers that have no message key of their own.
  const catalog = useQuery({
    queryKey: connectorKeys.catalog,
    queryFn: ({ signal }) => getConnectorCatalog(signal),
    enabled: Boolean(source.provider),
    staleTime: 5 * 60_000,
  });
  const catalogEntry = catalog.data?.find((entry) => entry.provider_id === source.provider);
  const effectiveRealtimeMode: RealtimeMode | null =
    source.type === 'manual' ? null : (realtimeMode ?? source.realtime_mode ?? catalogEntry?.realtime_tier ?? 'near_realtime');
  const collect = useCollectNow(source.id, csrfToken);
  const actions = useSourceActions({ source, activation: activation.data, onResumed: onEdit, onChanged, onPurgeStarted });
  const handoff = useRemovedFocusHandoff(onRemoved);
  const rowState = sourceRowState(source, activation.data);
  const hasDetail = [
    collect.collectResult,
    operationId,
    actions.error,
    collect.collect.error,
    activation.isError,
    collect.collectBlocked,
  ].some(Boolean);
  return (
    <>
      <tr className="border-b border-border max-[1023px]:block max-[1023px]:py-3">
        <SourceNameCell
          source={source}
          cellRef={handoff.nameCellRef}
          mode={effectiveRealtimeMode}
          catalogLabel={catalogEntry?.label}
          onChanged={onChanged}
        />
        <SourceStatusCell rowState={rowState} connector={connector} activation={activation.data} />
        <SourceTimingCells source={source} connector={connector} schedule={schedule} timezone={timezone} />
        <SourceActionsCell
          source={source}
          connector={connector}
          rowState={rowState}
          activationActive={activation.data?.state === 'active'}
          editedHere={editingId === source.id}
          purged={purged}
          actions={actions}
          collect={collect}
          handoff={handoff}
          onEdit={onEdit}
          onOpenHistory={() => setHistoryOpen(true)}
          historyOpen={historyOpen}
        />
      </tr>
      {hasDetail && (
        <SourceDetailRow
          operationId={operationId}
          onPurgeFinished={onPurgeFinished}
          collect={collect}
          error={actions.error}
          activationError={activation.isError}
        />
      )}
      {connector && <SourceHistoryDialog source={source} open={historyOpen} onOpenChange={setHistoryOpen} />}
    </>
  );
}
