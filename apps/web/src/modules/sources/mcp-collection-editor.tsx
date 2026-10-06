'use client';

import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { ApiError } from '@/core/api';
import { activateConnector, getConnectorConfiguration, updateSourceStatus } from './api';
import { listMcpConnections, listMcpGrants, saveMcpCollection, type McpCollectionConfig, type McpConnection, type McpGrant } from './mcp-api';

const intervals = [15, 30, 60, 360, 1440] as const;

/** Owner editor for selecting source-scoped reviewed MCP grants and activating their bounded schedule. */
export function McpCollectionEditor({ sourceId, sourceStatus, onChanged, onDraftChange }: { sourceId: string; sourceStatus: string; onChanged: () => void; onDraftChange: (dirty: boolean) => void }) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const [connections, setConnections] = useState<McpConnection[]>([]);
  const [grants, setGrants] = useState<McpGrant[]>([]);
  const [generation, setGeneration] = useState(0);
  const [connectionId, setConnectionId] = useState('');
  const visibleGrants = connectionId ? grants : [];
  const [grantIds, setGrantIds] = useState<string[]>([]);
  const [argumentsByGrant, setArgumentsByGrant] = useState<Record<string, string>>({});
  const [interval, setInterval] = useState<15 | 30 | 60 | 360 | 1440>(60);
  const [timezone, setTimezone] = useState('Asia/Ho_Chi_Minh');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<'saveFailed' | 'argumentsInvalid' | 'staleConflict' | ''>('');

  useEffect(() => {
    let current = true;
    void Promise.all([getConnectorConfiguration(sourceId), listMcpConnections()]).then(async ([saved, available]) => {
      if (!current) return;
      const config = saved.configuration as McpCollectionConfig;
      const connectionsNow = available.items.filter((item) => item.enabled);
      const selectedConnection = connectionsNow.find((item) => item.id === config.connection_id) ?? connectionsNow[0];
      setConnections(connectionsNow);
      setGeneration(saved.source_generation);
      setConnectionId(selectedConnection?.id ?? '');
      const savedCalls = selectedConnection?.id === config.connection_id ? config.calls ?? [] : [];
      setGrantIds(savedCalls.map((call) => call.grant_id));
      setArgumentsByGrant(Object.fromEntries(savedCalls.map((call) => [call.grant_id, JSON.stringify(call.arguments)])));
      setInterval(config.schedule_interval_minutes ?? 60);
      setTimezone(config.timezone ?? 'Asia/Ho_Chi_Minh');
    }).catch(() => { if (current) setError('saveFailed'); }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [sourceId]);

  useEffect(() => {
    let current = true;
    if (!connectionId) return;
    void listMcpGrants(connectionId).then((result) => {
      const connection = connections.find((item) => item.id === connectionId);
      if (current) setGrants(result.items.filter((grant) => grant.purpose === 'collection' && grant.revoked_at === null
        && (!grant.expires_at || new Date(grant.expires_at).getTime() > Date.now())
        && grant.source_ids.includes(sourceId) && connection !== undefined
        && grant.reviewed_connection_revision === connection.revision
        && grant.reviewed_profile_hash === connection.deployment_profile_hash));
    }).catch(() => { if (current) setError('saveFailed'); });
    return () => { current = false; };
  }, [connectionId, sourceId, connections]);

  async function save(activate: boolean) {
    if (busy || !connectionId || !grantIds.length || sourceStatus !== 'active') return;
    let calls: McpCollectionConfig['calls'];
    try {
      calls = grantIds.map((grant_id) => {
        const parsed: unknown = JSON.parse(argumentsByGrant[grant_id] || '{}');
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('invalid_arguments');
        return { grant_id, arguments: parsed as Record<string, unknown> };
      });
    }
    catch { setError('argumentsInvalid'); return; }
    setBusy(true); setError('');
    try {
      const config = { connection_id: connectionId, calls, schedule_interval_minutes: interval, timezone } as const;
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

  async function resume() {
    setBusy(true); setError('');
    try { await updateSourceStatus(sourceId, 'active', csrfToken); onChanged(); }
    catch { setError('saveFailed'); }
    finally { setBusy(false); }
  }

  return <section className="source-capability" aria-labelledby="mcp-collection-title">
    <h3 id="mcp-collection-title">{t('mcpCollectionTitle')}</h3>
    <p className="muted">{t('mcpCollectionHelp')}</p>
    {loading ? <p className="muted">{t('loading')}</p> : connections.length === 0 ? <p className="error">{t('mcpNoConnection')}</p> : <>
      <div className="field"><Label htmlFor="mcp-collection-connection">{t('mcpConnection')}</Label><Select disabled={busy} value={connectionId} onValueChange={(value) => { setConnectionId(value); setGrants([]); setGrantIds([]); onDraftChange(true); }}><SelectTrigger id="mcp-collection-connection"><SelectValue /></SelectTrigger><SelectContent>{connections.map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectContent></Select></div>
      <fieldset className="space-y-2" disabled={busy}><legend>{t('mcpCollectionGrants')}</legend>
        {visibleGrants.length === 0 ? <p className="muted">{t('mcpNoGrants')}</p> : visibleGrants.map((grant) => <div key={grant.id} className="space-y-2"><label className="field-inline"><Checkbox disabled={!grantIds.includes(grant.id) && grantIds.length >= 10} checked={grantIds.includes(grant.id)} onCheckedChange={(checked) => { setGrantIds((current) => checked ? [...new Set([...current, grant.id])] : current.filter((id) => id !== grant.id)); onDraftChange(true); }} /><span>{grant.capability_id} · {grant.risk}</span></label>{grantIds.includes(grant.id) && <div className="field"><Label htmlFor={`mcp-args-${grant.id}`}>{t('mcpArguments')}</Label><Input id={`mcp-args-${grant.id}`} value={argumentsByGrant[grant.id] ?? '{}'} onChange={(event) => { setArgumentsByGrant((current) => ({ ...current, [grant.id]: event.target.value })); onDraftChange(true); }} /></div>}</div>)}
      </fieldset>
      <div className="field"><Label htmlFor="mcp-collection-schedule">{t('schedule')}</Label><Select disabled={busy} value={String(interval)} onValueChange={(value) => { setInterval(Number(value) as typeof interval); onDraftChange(true); }}><SelectTrigger id="mcp-collection-schedule"><SelectValue /></SelectTrigger><SelectContent>{intervals.map((minutes) => <SelectItem key={minutes} value={String(minutes)}>{t('everyMinutes', { minutes })}</SelectItem>)}</SelectContent></Select></div>
      <div className="field"><Label htmlFor="mcp-collection-timezone">{t('timezone')}</Label><Input disabled={busy} id="mcp-collection-timezone" value={timezone} onChange={(event) => { setTimezone(event.target.value); onDraftChange(true); }} maxLength={64} /></div>
      {sourceStatus === 'paused' && <Button className="secondary" disabled={busy} onClick={resume}>{t('resume')}</Button>}
      {error === 'staleConflict' && <div role="alert"><p className="error">{t('mcpCollectionStaleConflict')}</p><Button className="secondary" disabled={busy} onClick={reloadConflict}>{t('mcpCollectionReload')}</Button></div>}
      <div className="form-actions"><Button className="secondary" disabled={busy || error === 'staleConflict' || !grantIds.length || sourceStatus !== 'active'} onClick={() => void save(false)}>{busy ? t('saving') : t('save')}</Button><Button disabled={busy || error === 'staleConflict' || !grantIds.length || sourceStatus !== 'active'} onClick={() => void save(true)}>{busy ? t('enabling') : t('saveEnable')}</Button></div>
    </>}
    {error && error !== 'staleConflict' && <p className="error" role="alert">{t(error === 'argumentsInvalid' ? 'mcpArgumentsInvalid' : 'mcpCollectionSaveFailed')}</p>}
  </section>;
}
