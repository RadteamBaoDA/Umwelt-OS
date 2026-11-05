'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useDisplayPreferences } from '@/core/query-provider';
import { useBrowserTimezone } from '../shared/use-browser-timezone';
import { useResetOnChange } from '../shared/use-reset-on-change';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { CheckboxField, FieldGrid, SettingsActions, SettingsSection } from '@/components/settings-layout';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { ApiError } from '@/core/api';
import { activateConnector, getConnectorConfiguration, updateSourceStatus } from './api';
import { listMcpConnections, listMcpGrants, saveMcpCollection, type McpCollectionConfig, type McpConnection, type McpGrant } from './mcp-api';

const intervals = [15, 30, 60, 360, 1440] as const;

type ScheduleMinutes = 15 | 30 | 60 | 360 | 1440;
type ActionError = 'saveFailed' | 'argumentsInvalid' | 'staleConflict' | '';

/**
 * Editable draft of an MCP collection plus its two read queries.
 * The configuration query runs once per mount (gcTime 0, no retry/refocus) so the editor always starts from the server's
 * current draft; react-query aborts it through the signal when the editor unmounts or the source changes. The loaded
 * payload is copied into the draft once per fetched payload. Grants are fetched per selected connection and filtered to
 * reviewed, unexpired collection grants for this source. `acknowledgeLoadErrors` records the current load-error timestamps
 * so a save/resume attempt clears only the errors present at click time (a later, different failure still shows).
 */
function useMcpCollectionDraft(sourceId: string) {
  const [connections, setConnections] = useState<McpConnection[]>([]);
  const [generation, setGeneration] = useState(0);
  const [connectionId, setConnectionId] = useState('');
  const [grantIds, setGrantIds] = useState<string[]>([]);
  const [argumentsByGrant, setArgumentsByGrant] = useState<Record<string, string>>({});
  const [scheduleMinutes, setScheduleMinutes] = useState<ScheduleMinutes>(60);
  const display = useDisplayPreferences();
  // Browser zone first (as in mcp-editor); the preference zone may still be the bootstrap default while preferences load.
  const browserTimezone = useBrowserTimezone(display.timezone);
  const [timezone, setTimezone] = useState(browserTimezone);
  // A save/resume attempt acknowledges (clears) the load errors that exist at click time, matching the old imperative
  // setError(''). Acknowledgement is keyed by each query's errorUpdatedAt so a later, different failure still shows.
  const [acknowledged, setAcknowledged] = useState<{ config: number; grants: number } | null>(null);

  const configQuery = useQuery({
    queryKey: ['mcp-collection-editor', sourceId],
    queryFn: async ({ signal }) => {
      const [saved, available] = await Promise.all([getConnectorConfiguration(sourceId, signal), listMcpConnections(signal)]);
      return { saved, available };
    },
    gcTime: 0, staleTime: 0, retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false, refetchOnMount: false,
  });
  // Form reset: copy the loaded server configuration into the editable draft once per fetched payload.
  useResetOnChange([configQuery.data], () => {
    if (!configQuery.data) return;
    const { saved, available } = configQuery.data;
    const config = saved.configuration as McpCollectionConfig;
    const connectionsNow = available.items.filter((item) => item.enabled);
    const selectedConnection = connectionsNow.find((item) => item.id === config.connection_id) ?? connectionsNow[0];
    setConnections(connectionsNow);
    setGeneration(saved.source_generation);
    setConnectionId(selectedConnection?.id ?? '');
    const savedCalls = selectedConnection?.id === config.connection_id ? config.calls ?? [] : [];
    setGrantIds(savedCalls.map((call) => call.grant_id));
    setArgumentsByGrant(Object.fromEntries(savedCalls.map((call) => [call.grant_id, JSON.stringify(call.arguments)])));
    setScheduleMinutes(config.schedule_interval_minutes ?? 60);
    // Default to the browser timezone (same source mcp-editor uses) rather than a fixed region.
    setTimezone(config.timezone ?? browserTimezone);
  });

  const selectedConnection = connections.find((item) => item.id === connectionId);
  const grantsQuery = useQuery({
    queryKey: ['mcp-collection-grants', connectionId, sourceId, selectedConnection?.revision, selectedConnection?.deployment_profile_hash],
    queryFn: async ({ signal }) => {
      const result = await listMcpGrants(connectionId, signal);
      return result.items.filter((grant) => grant.purpose === 'collection' && grant.revoked_at === null
        && (!grant.expires_at || new Date(grant.expires_at).getTime() > Date.now())
        && grant.source_ids.includes(sourceId) && selectedConnection !== undefined
        && grant.reviewed_connection_revision === selectedConnection.revision
        && grant.reviewed_profile_hash === selectedConnection.deployment_profile_hash);
    },
    enabled: Boolean(connectionId) && selectedConnection !== undefined,
    gcTime: 0, staleTime: 0, retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
  });
  const visibleGrants: McpGrant[] = connectionId ? grantsQuery.data ?? [] : [];
  const loadFailed = (configQuery.isError && configQuery.errorUpdatedAt !== acknowledged?.config)
    || (grantsQuery.isError && grantsQuery.errorUpdatedAt !== acknowledged?.grants);
  /** Record the current load-error timestamps so only errors present now are treated as acknowledged. */
  const acknowledgeLoadErrors = () => setAcknowledged({ config: configQuery.errorUpdatedAt, grants: grantsQuery.errorUpdatedAt });
  return {
    loading: configQuery.isPending, loadFailed, acknowledgeLoadErrors, connections, visibleGrants, generation, setGeneration,
    connectionId, setConnectionId, grantIds, setGrantIds, argumentsByGrant, setArgumentsByGrant, scheduleMinutes, setScheduleMinutes, timezone, setTimezone,
  };
}

/** Parses each selected grant's JSON arguments; throws `invalid_arguments` unless every value is a JSON object. */
function parseCalls(grantIds: string[], argumentsByGrant: Record<string, string>): McpCollectionConfig['calls'] {
  return grantIds.map((grant_id) => {
    const parsed: unknown = JSON.parse(argumentsByGrant[grant_id] || '{}');
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('invalid_arguments');
    return { grant_id, arguments: parsed as Record<string, unknown> };
  });
}

type Draft = ReturnType<typeof useMcpCollectionDraft>;

/** Checkbox list of the reviewed grants (capped at 10 selections) with a JSON-arguments input under each selected grant. */
function GrantChecklist({ draft, busy, onDraftChange }: { draft: Draft; busy: boolean; onDraftChange: (dirty: boolean) => void }) {
  const t = useTranslations('sources');
  const { visibleGrants, grantIds, setGrantIds, argumentsByGrant, setArgumentsByGrant } = draft;
  return <div role="group" aria-label={t('mcpCollectionGrants')} className="flex flex-col gap-2"><h4 className="m-0 text-sm font-semibold">{t('mcpCollectionGrants')}</h4>
    {visibleGrants.length === 0 ? <p className="text-muted-foreground">{t('mcpNoGrants')}</p> : visibleGrants.map((grant) => <div key={grant.id} className="space-y-2"><CheckboxField><Checkbox id={`mcp-grant-${grant.id}`} disabled={busy || (!grantIds.includes(grant.id) && grantIds.length >= 10)} checked={grantIds.includes(grant.id)} onCheckedChange={(checked) => { setGrantIds((current) => checked ? [...new Set([...current, grant.id])] : current.filter((id) => id !== grant.id)); onDraftChange(true); }} /><Label htmlFor={`mcp-grant-${grant.id}`}>{grant.capability_id} · {grant.risk}</Label></CheckboxField>{grantIds.includes(grant.id) && <div className="field"><Label htmlFor={`mcp-args-${grant.id}`}>{t('mcpArguments')}</Label><Input id={`mcp-args-${grant.id}`} value={argumentsByGrant[grant.id] ?? '{}'} onChange={(event) => { setArgumentsByGrant((current) => ({ ...current, [grant.id]: event.target.value })); onDraftChange(true); }} /></div>}</div>)}
  </div>;
}

/** Schedule interval and timezone fields of the collection draft. */
function ScheduleFields({ draft, busy, onDraftChange }: { draft: Draft; busy: boolean; onDraftChange: (dirty: boolean) => void }) {
  const t = useTranslations('sources');
  const { scheduleMinutes, setScheduleMinutes, timezone, setTimezone } = draft;
  return <FieldGrid><div className="field"><Label htmlFor="mcp-collection-schedule">{t('schedule')}</Label><Select disabled={busy} value={String(scheduleMinutes)} onValueChange={(value) => { setScheduleMinutes(Number(value) as ScheduleMinutes); onDraftChange(true); }}><SelectTrigger id="mcp-collection-schedule"><SelectValue /></SelectTrigger><SelectContent>{intervals.map((minutes) => <SelectItem key={minutes} value={String(minutes)}>{t('everyMinutes', { minutes })}</SelectItem>)}</SelectContent></Select></div>
    <div className="field"><Label htmlFor="mcp-collection-timezone">{t('timezone')}</Label><Input disabled={busy} id="mcp-collection-timezone" value={timezone} onChange={(event) => { setTimezone(event.target.value); onDraftChange(true); }} maxLength={64} /></div></FieldGrid>;
}

/** Owner editor for selecting source-scoped reviewed MCP grants and activating their bounded schedule. */
export function McpCollectionEditor({ sourceId, sourceStatus, onChanged, onDraftChange }: { sourceId: string; sourceStatus: string; onChanged: () => void; onDraftChange: (dirty: boolean) => void }) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const draft = useMcpCollectionDraft(sourceId);
  const { connections, connectionId, setConnectionId, grantIds, setGrantIds, generation, setGeneration } = draft;
  const [busy, setBusy] = useState(false);
  const [actionError, setError] = useState<ActionError>('');
  const error = actionError || (draft.loadFailed ? 'saveFailed' : '');
  const saveBlocked = busy || error === 'staleConflict' || !grantIds.length || sourceStatus !== 'active';

  /**
   * Saves the draft against the last seen source generation (409 shows the stale-conflict state) and optionally activates
   * it with the saved revision. Invalid JSON arguments abort before any request. Guarded against re-entry while busy.
   */
  async function save(activate: boolean) {
    if (busy || !connectionId || !grantIds.length || sourceStatus !== 'active') return;
    let calls: McpCollectionConfig['calls'];
    try { calls = parseCalls(grantIds, draft.argumentsByGrant); }
    catch { setError('argumentsInvalid'); return; }
    setBusy(true); setError(''); draft.acknowledgeLoadErrors();
    try {
      const config = { connection_id: connectionId, calls, schedule_interval_minutes: draft.scheduleMinutes, timezone: draft.timezone } as const;
      let saved: Awaited<ReturnType<typeof saveMcpCollection>>;
      try {
        saved = await saveMcpCollection(sourceId, generation, config, csrfToken);
      } catch (cause) {
        setError(cause instanceof ApiError && cause.status === 409 ? 'staleConflict' : 'saveFailed');
        return;
      }
      setGeneration(saved.source_generation);
      onDraftChange(false);
      if (activate) await activateConnector(sourceId, saved.expected_revision, 'keep', undefined, csrfToken);
      onChanged();
    } catch {
      setError('saveFailed');
    } finally { setBusy(false); }
  }

  /** Discard this stale draft and reload the current source settings before editing again. */
  function reloadConflict() {
    window.location.reload();
  }

  /** Resumes a paused source (status -> active) and reports the change. */
  async function resume() {
    setBusy(true); setError(''); draft.acknowledgeLoadErrors();
    try { await updateSourceStatus(sourceId, 'active', csrfToken); onChanged(); }
    catch { setError('saveFailed'); }
    finally { setBusy(false); }
  }

  return <SettingsSection title={t('mcpCollectionTitle')} description={t('mcpCollectionHelp')}>
    {draft.loading ? <p className="text-muted-foreground">{t('loading')}</p> : connections.length === 0 ? <p className="error">{t('mcpNoConnection')}</p> : <>
      <div className="field"><Label htmlFor="mcp-collection-connection">{t('mcpConnection')}</Label><Select disabled={busy} value={connectionId} onValueChange={(value) => { setConnectionId(value); setGrantIds([]); onDraftChange(true); }}><SelectTrigger id="mcp-collection-connection"><SelectValue /></SelectTrigger><SelectContent>{connections.map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectContent></Select></div>
      <GrantChecklist draft={draft} busy={busy} onDraftChange={onDraftChange} />
      <ScheduleFields draft={draft} busy={busy} onDraftChange={onDraftChange} />
      {sourceStatus === 'paused' && <Button variant="outline" disabled={busy} onClick={resume}>{t('resume')}</Button>}
      {error === 'staleConflict' && <div role="alert"><p className="error">{t('mcpCollectionStaleConflict')}</p><Button variant="outline" disabled={busy} onClick={reloadConflict}>{t('mcpCollectionReload')}</Button></div>}
      <SettingsActions><Button variant="outline" disabled={saveBlocked} onClick={() => void save(false)}>{busy ? t('saving') : t('save')}</Button><Button disabled={saveBlocked} onClick={() => void save(true)}>{busy ? t('enabling') : t('saveEnable')}</Button></SettingsActions>
    </>}
    {error && error !== 'staleConflict' && <p className="error" role="alert">{t(error === 'argumentsInvalid' ? 'mcpArgumentsInvalid' : 'mcpCollectionSaveFailed')}</p>}
  </SettingsSection>;
}
