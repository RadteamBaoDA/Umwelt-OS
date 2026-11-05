'use client';

import { Fragment } from 'react';
import Link from 'next/link';
import { ChevronRight } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { CheckboxField, FieldGrid } from '@/components/settings-layout';
import { Input } from '@/components/ui/input';
import { PasswordInput } from '@/components/ui/password-input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { TablePagination } from '@/components/ui/pagination';
import { Table, TableBody, TableHead, TableHeader } from '@/components/ui/table';
import { disableMcpConnection, enableMcpConnection } from './mcp-api';
import { formatDateTime } from '@/core/format';
import { useMcpSettings, fromConnection, healthMessageKey, grantPurposeMessageKey, grantRiskMessageKey, type Draft, type McpMessageKey } from './use-mcp-settings';

const CONFIRM_KEYS = {
  rotate: { title: 'confirmRotate', description: 'rotateWarning' },
  revoke: { title: 'confirmRevoke', description: 'revokeWarning' },
  'save-empty-grants': { title: 'confirmEmptyGrant', description: 'emptyGrantWarning' },
  discover: { title: 'confirmDiscover', description: 'rediscoverConfirm' },
  dismiss: { title: 'confirmDismiss', description: 'dismissWarning' },
} as const;

type McpModel = ReturnType<typeof useMcpSettings>;

/** Error, reconciliation/conflict and retained-draft notices above the connection list. */
function McpStatusBanners({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { dirty, grantDirty, busy, loading, conflict, reconciliationRequired, error, reloadServer, reconcileMetadata } = m;
  return <>
    {error && <p className="error" role="alert">{t(error as McpMessageKey)}</p>}
    {(reconciliationRequired || conflict) && <p className="text-muted-foreground" role="status">{t(conflict ? 'conflict' : 'reconciliationRequired')} <Button variant="outline" disabled={busy || loading} onClick={() => void reconcileMetadata()}>{t('refreshMetadata')}</Button></p>}
    {(dirty || grantDirty) && <p className="text-muted-foreground" role="status">{t('draftRetained')} <Button variant="outline" disabled={busy} onClick={reloadServer}>{t('reloadDiscard')}</Button></p>}
  </>;
}

/** Connections heading with the new-connection button, and the selectable connection list. */
function ConnectionList({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { connections, selected, dirty, grantDirty, tokenView, confirm, busy, loading, reconciliationRequired, choose, startNewConnection } = m;
  return <>
      <div className="flex flex-wrap items-center justify-between gap-3"><div className="flex flex-col gap-1"><h2 id="mcp-connections-title" className="m-0 text-lg font-semibold">{t('connections')}</h2><p className="m-0 text-sm text-muted-foreground">{t('connectionOrder')}</p></div><Button disabled={busy || dirty || grantDirty || Boolean(confirm) || Boolean(tokenView)} onClick={startNewConnection}>{t('newConnection')}</Button></div>
      {loading ? <p role="status">{t('loading')}</p> : connections.length === 0 ? <p className="m-0 text-sm text-muted-foreground">{t('emptyConnections')}</p> : <ul className="record-list">{connections.map((item) => <li key={item.id} className="record-row flex flex-wrap items-center justify-between gap-3"><div><strong>{item.name}</strong><p className="text-muted-foreground">{t(item.transport === 'stdio' ? 'transportStdio' : 'transportStreamableHttp')} · {t(item.enabled ? 'enabled' : 'disabled')}</p></div><Button variant="outline" disabled={busy || loading || reconciliationRequired || Boolean(confirm) || Boolean(tokenView)} aria-pressed={selected?.id === item.id} onClick={() => choose(item)}>{t(selected?.id === item.id ? 'selected' : 'select')}</Button></li>)}</ul>}
  </>;
}

/** Disable (when enabled) or enable (when reviewed and ready) the selected connection. */
function ToggleEnabledButton({ m, selected }: { m: McpModel; selected: NonNullable<McpModel['selected']> }) {
  const t = useTranslations('mcp');
  const { csrfToken, dirty, grantDirty, tokenView, perform, canEnable, inputsFrozen } = m;
  return <>
      {selected.enabled ? <Button variant="outline" disabled={inputsFrozen || dirty || grantDirty || Boolean(tokenView)} onClick={() => void perform(() => disableMcpConnection(selected.id, selected.revision, csrfToken))}>{t('disable')}</Button> : <Button disabled={inputsFrozen || dirty || grantDirty || Boolean(tokenView) || !canEnable} onClick={() => void perform(() => enableMcpConnection(selected.id, selected.revision, csrfToken))}>{t('enable')}</Button>}
  </>;
}

/** Check, discover, edit and enable/disable buttons for the selected connection. */
function ConnectionActions({ m, selected }: { m: McpModel; selected: NonNullable<McpModel['selected']> }) {
  const t = useTranslations('mcp');
  const { setDraft, dirty, setDirty, grantDirty, setEditing, tokenView, confirm, setConflict, setDraftOwner, check, discover, inputsFrozen } = m;
  return <>
      <div className="flex flex-wrap gap-2"><Button variant="outline" disabled={inputsFrozen || dirty || grantDirty || Boolean(confirm)} onClick={check}>{t('checkSaved')}</Button><Button variant="outline" disabled={inputsFrozen || dirty || grantDirty || Boolean(confirm)} onClick={discover}>{t('discover')}</Button><Button variant="outline" disabled={inputsFrozen || dirty || grantDirty || Boolean(confirm) || Boolean(tokenView)} onClick={() => { setDraft(fromConnection(selected)); setDraftOwner({ id: selected.id, baseRevision: selected.revision }); setEditing(true); setDirty(false); setConflict(false); }}>{t('edit')}</Button><ToggleEnabledButton m={m} selected={selected} /></div>
  </>;
}

/** Selected connection summary (health, last update) and its action buttons. */
function SelectedConnectionHeader({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { display, selected } = m;
  return <>
      {selected && <div className="flex flex-wrap justify-between gap-2"><div><h4>{selected.name}</h4><p className="text-muted-foreground">{t('health')}: {t(healthMessageKey(selected.health))} · {selected.error_code && selected.error_code !== 'connected' ? <>{t('reportedCode')}: <code>{selected.error_code}</code></> : t('noError')}</p><p className="text-muted-foreground">{t('updatedAt')}: {formatDateTime(selected.updated_at, display.locale, display.timezone)} · {display.timezone}</p></div><ConnectionActions m={m} selected={selected} /></div>}
  </>;
}

/** Editable connection draft form (name, transport, target, auth, credential, timeout) with save and cancel. */
function ConnectionEditorForm({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { selected, draft, setDraft, dirty, setDirty, editing, setEditing, busy, setConflict, setDraftOwner, save, inputsFrozen } = m;
  return <>
      {editing && draft && <div className="grid grid-cols-1 gap-4 min-[720px]:grid-cols-2" role="group" aria-label={t('connectionEditor')}>
        <div><Label htmlFor="mcp-name">{t('name')}</Label><Input disabled={inputsFrozen} id="mcp-name" value={draft.name} onChange={(e) => { setDraft({ ...draft, name: e.target.value }); setDirty(true); }} maxLength={120} /></div>
        <div><Label htmlFor="mcp-transport">{t('transport')}</Label><Select disabled={inputsFrozen} value={draft.transport} onValueChange={(value: Draft['transport']) => { setDraft({ ...draft, transport: value, endpoint: '', profile: '', auth: value === 'stdio' ? 'none' : draft.auth, credentialAction: value === 'stdio' && draft.auth === 'bearer' ? 'remove' : draft.credentialAction }); setDirty(true); }}><SelectTrigger id="mcp-transport"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="streamable_http">{t('transportStreamableHttp')}</SelectItem><SelectItem value="stdio">{t('transportStdio')}</SelectItem></SelectContent></Select></div>
        {draft.transport === 'streamable_http' ? <div><Label htmlFor="mcp-endpoint">{t('endpoint')}</Label><Input disabled={inputsFrozen} id="mcp-endpoint" value={draft.endpoint} onChange={(e) => { setDraft({ ...draft, endpoint: e.target.value }); setDirty(true); }} maxLength={2048} /></div> : <div><Label htmlFor="mcp-profile">{t('deploymentProfile')}</Label><Input disabled={inputsFrozen} id="mcp-profile" value={draft.profile} onChange={(e) => { setDraft({ ...draft, profile: e.target.value }); setDirty(true); }} maxLength={80} /><p className="text-muted-foreground">{t('profileAdminOnly')}</p></div>}
        <div><Label htmlFor="mcp-auth">{t('authentication')}</Label><Select disabled={inputsFrozen || draft.transport === 'stdio'} value={draft.auth} onValueChange={(value: Draft['auth']) => { setDraft({ ...draft, auth: value, credentialAction: value === 'none' ? 'remove' : draft.credentialAction === 'remove' ? 'replace' : draft.credentialAction }); setDirty(true); }}><SelectTrigger id="mcp-auth"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="none">{t('authNone')}</SelectItem><SelectItem value="bearer">{t('authBearer')}</SelectItem></SelectContent></Select></div>
        {draft.auth === 'bearer' && <><div><Label htmlFor="mcp-credential-action">{t('credentialAction')}</Label><Select disabled={inputsFrozen} value={draft.credentialAction} onValueChange={(value: Draft['credentialAction']) => { setDraft({ ...draft, credentialAction: value, credential: '' }); setDirty(true); }}><SelectTrigger id="mcp-credential-action"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="retain">{t('retain')}</SelectItem><SelectItem value="replace">{t('replace')}</SelectItem><SelectItem value="remove" disabled>{t('removeUnavailable')}</SelectItem></SelectContent></Select></div>{draft.credentialAction === 'replace' && <div><Label htmlFor="mcp-secret">{t('credential')}</Label><PasswordInput disabled={inputsFrozen} id="mcp-secret" autoComplete="new-password" value={draft.credential} onChange={(e) => { setDraft({ ...draft, credential: e.target.value }); setDirty(true); }} /></div>}</>}
        <div><Label htmlFor="mcp-timeout">{t('timeout')}</Label><Input disabled={inputsFrozen} id="mcp-timeout" type="number" min={1} max={60} value={draft.timeout} onChange={(e) => { setDraft({ ...draft, timeout: e.target.value }); setDirty(true); }} /></div>
        {draft.transport === 'stdio' && selected?.credential_configured && <p className="text-muted-foreground sm:col-span-2">{t('stdioCredentialMustRemove')} <CheckboxField><Checkbox id="mcp-remove-stored-credential" disabled={inputsFrozen} checked={draft.credentialAction === 'remove'} onCheckedChange={(checked) => { setDraft({ ...draft, credentialAction: checked ? 'remove' : 'retain' }); setDirty(true); }} /><Label htmlFor="mcp-remove-stored-credential">{t('removeStoredCredential')}</Label></CheckboxField></p>}
        <p className="text-muted-foreground sm:col-span-2">{t('saveDisables')}</p><div className="flex gap-2 sm:col-span-2"><Button disabled={inputsFrozen || !dirty} onClick={() => void save()}>{t('save')}</Button><Button variant="outline" disabled={busy} onClick={() => { setDraft(selected ? fromConnection(selected) : null); setDraftOwner({ id: selected?.id ?? null, baseRevision: selected?.revision ?? null }); setDirty(false); setEditing(false); setConflict(false); }}>{t('cancel')}</Button></div>
      </div>}
  </>;
}

/** Read-only list of the grants saved for the selected connection. */
function SavedGrants({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { grants } = m;
  return <>
      {grants.length > 0 && <div><h4>{t('savedGrants')}</h4><ul className="record-list">{grants.map((grant) => <li key={grant.id} className="record-row"><strong>{t(grantPurposeMessageKey(grant.purpose))} · {t(grantRiskMessageKey(grant.risk))} · {grant.revoked_at ? t('revoked') : t('grantLive')}</strong><p className="text-muted-foreground">{grant.destinations.join(', ')}</p></li>)}</ul><p className="text-muted-foreground">{t('rediscoverToReview')}</p></div>}
  </>;
}

/** Discovered descriptor review table where the owner scopes read-only grants per capability. */
function DescriptorReview({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { selected, discovery, capabilityPager, grantDrafts, sources, tokenView, browserTimezone, saveGrants, updateGrant, supported, inputsFrozen } = m;
  return <>
      {discovery && <div className="space-y-3"><h4>{t('descriptorReview')}</h4>
        <p className="text-muted-foreground">{t('discoveryNotGrant')} {t('permLevelsNote')}</p>
        <p className="text-muted-foreground">{t('sourceScopeNote')} {t('scopeBoundsHelp')}</p>
        <div className="max-[720px]:overflow-visible">
          <Table className="border-collapse text-left max-[720px]:block" containerClassName="max-[720px]:max-h-none">
            <caption className="sr-only">{t('toolsCaption')}</caption>
            <TableHeader className="max-[720px]:sr-only"><tr className="border-b border-border">{(['toolColName', 'toolColRisk', 'toolColPermission'] as const).map((key) => <TableHead key={key} scope="col" className="text-muted-foreground px-3 py-2 text-xs font-semibold">{t(key)}</TableHead>)}</tr></TableHeader>
            <TableBody className="max-[720px]:block">
              {capabilityPager.pageItems.map((item) => {
                const value = grantDrafts[item.id] ?? { selected: false, purpose: 'chat' as const, sources: [], destinations: '', expires: '' };
                const level = value.selected && supported(item) ? value.purpose : 'none';
                const description = typeof item.descriptor.description === 'string' ? item.descriptor.description : null;
                return <Fragment key={item.id}>
                  <tr className="border-t border-border max-[720px]:mb-2 max-[720px]:block max-[720px]:rounded-lg max-[720px]:border max-[720px]:p-3">
                    <th scope="row" className="px-3 py-3 text-left align-top font-normal max-[720px]:block max-[720px]:px-0"><Label htmlFor={`purpose-${item.id}`}>{item.kind}: {item.remote_key}</Label>{description && <p className="text-muted-foreground text-sm">{description}</p>}</th>
                    <td className="px-3 py-3 align-top max-[720px]:block max-[720px]:px-0"><span className="badge">{supported(item) ? t('grantReadOnlyOnly') : t('unsupportedShort')}</span></td>
                    <td className="px-3 py-3 align-top max-[720px]:block max-[720px]:px-0">
                      <Select disabled={inputsFrozen || !supported(item)} value={level} onValueChange={(next: 'none' | 'chat' | 'collection') => updateGrant(item.id, next === 'none' ? { selected: false } : { selected: true, purpose: next })}>
                        <SelectTrigger id={`purpose-${item.id}`} className="min-h-9 pointer-coarse:min-h-11"><SelectValue /></SelectTrigger>
                        <SelectContent><SelectItem value="none">{t('permNone')}</SelectItem><SelectItem value="chat">{t('permChat')}</SelectItem><SelectItem value="collection">{t('permCollection')}</SelectItem></SelectContent>
                      </Select>
                    </td>
                  </tr>
                  <tr className="max-[720px]:block"><td colSpan={3} className="px-3 pb-3 max-[720px]:block max-[720px]:px-0">
                    <p className="text-muted-foreground text-sm">{supported(item) ? t('readOnlyGrant') : t('unsupportedKind')}</p>
                    <details><summary className="cursor-pointer text-sm">{t('descriptorShow')}</summary><p className="text-muted-foreground">{t('descriptorHash')}: <code>{item.descriptor_hash}</code></p><pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-2 text-xs">{JSON.stringify(item.descriptor, null, 2)}</pre></details>
                    {value.selected && supported(item) && <div className="mt-2 grid gap-2 sm:grid-cols-2"><div role="group" aria-label={t('sourceScopes')} className="flex flex-col gap-2"><p className="m-0 text-sm font-semibold">{t('sourceScopes')}</p>{sources.map((source) => <CheckboxField key={source.id}><Checkbox id={`scope-${item.id}-${source.id}`} disabled={inputsFrozen} checked={value.sources.includes(source.id)} onCheckedChange={(checked) => updateGrant(item.id, { sources: checked ? [...value.sources, source.id] : value.sources.filter((id) => id !== source.id) })} /><Label htmlFor={`scope-${item.id}-${source.id}`}>{source.name}</Label></CheckboxField>)}</div><div>{value.purpose === 'collection' ? <p className="text-muted-foreground">{t('collectionDestination')}</p> : <><Label htmlFor={`dest-${item.id}`}>{t('destinations')}</Label><Input disabled={inputsFrozen} id={`dest-${item.id}`} value={value.destinations} placeholder={t('destinationPlaceholder')} onChange={(e) => updateGrant(item.id, { destinations: e.target.value })} /><p className="text-muted-foreground">{t('destinationPrivacy')}</p></>}<Label htmlFor={`expire-${item.id}`}>{t('optionalExpiry')}</Label><Input disabled={inputsFrozen} id={`expire-${item.id}`} type="datetime-local" value={value.expires} onChange={(e) => updateGrant(item.id, { expires: e.target.value })} /><p className="text-muted-foreground">{t('inputTimeZone', { timezone: browserTimezone })}</p></div>{value.purpose === 'collection' && <p className="text-muted-foreground sm:col-span-2">{t('collectionSourceLimit')}</p>}</div>}
                  </td></tr>
                </Fragment>;
              })}
            </TableBody>
          </Table>
          <TablePagination {...capabilityPager} />
        </div>
        <Button disabled={inputsFrozen || !selected || Boolean(tokenView)} onClick={saveGrants}>{t('saveGrantReview')}</Button>
      </div>}
  </>;
}

/** Technical identity details of the selected connection and the link to collection sources. */
function ConnectionTechnicalDetails({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { selected, discovery, grants } = m;
  return <>
      {selected && <details><summary className="cursor-pointer text-sm">{t('technicalDetails')}</summary><div className="space-y-1 text-muted-foreground">
        <p>{t('revision', { revision: selected.revision })}</p>
        {selected.transport === 'stdio' && <p>{t('profileHash')}: <code>{selected.deployment_profile_hash ?? t('unknown')}</code></p>}
        {discovery && <p>{t('discoveryIdentity', { revision: discovery.connection_revision, schema: discovery.schema_set_hash, profile: discovery.deployment_profile_hash ?? t('notApplicable') })}</p>}
        {grants.map((grant) => <p key={grant.id}>{t('reviewIdentity')}: r{grant.reviewed_connection_revision} · <code>{grant.descriptor_hash}</code></p>)}
      </div></details>}
      <p className="text-muted-foreground">{t('collectionUnavailable')} <Link className="underline" href="/settings/sources">{t('manageCollectionSources')}</Link></p>
  </>;
}

/** Inbound client name and expiry fields with the bounds note. */
function InboundIdentityFields({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { clientName, setClientName, clientExpiry, setClientExpiry, capabilities, setCapabilities, tokenView, busy, loading, conflict, reconciliationRequired, browserTimezone } = m;
  return <>
      <div className="grid gap-3 sm:grid-cols-2"><div><Label htmlFor="inbound-name">{t('name')}</Label><Input disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView)} id="inbound-name" value={clientName} onChange={(e) => setClientName(e.target.value)} maxLength={120} /></div><div><Label htmlFor="inbound-expiry">{t('clientExpiry')}</Label><Input disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView)} id="inbound-expiry" type="datetime-local" value={clientExpiry} onChange={(e) => setClientExpiry(e.target.value)} /><p className="text-muted-foreground">{t('inputTimeZone', { timezone: browserTimezone })}</p></div><div><Label htmlFor="inbound-capabilities">{t('capabilityLabels')}</Label><Input disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView)} id="inbound-capabilities" value={capabilities} onChange={(e) => setCapabilities(e.target.value)} placeholder={t('emptyAllowed')} /><p className="text-muted-foreground">{t('capabilityNoInference')}</p></div></div>
      <p className="text-muted-foreground">{t('inboundBounds')}</p>
  </>;
}

/** Native tool checkboxes (fingerprint-bound) for a new inbound client. */
function NativeToolBindings({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { tools, chosenTools, setChosenTools, tokenView, inputsFrozen } = m;
  return <>
      <div role="group" aria-label={t('nativeToolBindings')} className="flex flex-col gap-2"><h4 className="m-0 text-sm font-semibold">{t('nativeToolBindings')}</h4><FieldGrid>{tools.map((tool) => <CheckboxField key={`${tool.name}@${tool.version}`}><Checkbox id={`tool-${tool.name}@${tool.version}`} disabled={inputsFrozen || Boolean(tokenView)} checked={chosenTools.includes(`${tool.name}@${tool.version}`)} onCheckedChange={(checked) => setChosenTools((values) => checked ? [...values, `${tool.name}@${tool.version}`] : values.filter((entry) => entry !== `${tool.name}@${tool.version}`))} /><Label htmlFor={`tool-${tool.name}@${tool.version}`}>{tool.name} · {tool.version}<small className="block text-muted-foreground">{tool.permissions.join(', ') || t('noPermissions')}</small></Label></CheckboxField>)}</FieldGrid></div>
  </>;
}

/** Remote-safe source scope checkboxes for a new inbound client. */
function RemoteSourceScope({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { sources, sourcesTruncated, clientSourceIds, setClientSourceIds, tokenView, inputsFrozen } = m;
  return <>
      <div role="group" aria-label={t('remoteSourceScope')} className="flex flex-col gap-2"><h4 className="m-0 text-sm font-semibold">{t('remoteSourceScope')}</h4>{sources.length === 0 && <p className="text-muted-foreground">{t('noRemoteSources')}</p>}{sources.map((source) => <CheckboxField key={source.id}><Checkbox id={`client-source-${source.id}`} disabled={inputsFrozen || Boolean(tokenView)} checked={clientSourceIds.includes(source.id)} onCheckedChange={(checked) => setClientSourceIds((values) => checked ? [...values, source.id] : values.filter((id) => id !== source.id))} /><Label htmlFor={`client-source-${source.id}`}>{source.name}</Label></CheckboxField>)}{sourcesTruncated && <p className="text-muted-foreground">{t('sourceListBound')}</p>}</div>
  </>;
}

/** Issues the inbound client once; disabled while another action or token is pending. */
function IssueClientButton({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { tokenView, confirm, busy, loading, conflict, reconciliationRequired, issue } = m;
  return <>
      <Button disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView) || Boolean(confirm)} onClick={issue}>{t('issueClient')}</Button>
  </>;
}

/** Inbound client issuance form. */
function InboundIssueForm({ m }: { m: McpModel }) {
  return <>
      <InboundIdentityFields m={m} />
      <NativeToolBindings m={m} />
      <RemoteSourceScope m={m} />
      <IssueClientButton m={m} />
  </>;
}

/** Issued inbound clients with their rotate/revoke actions. */
function InboundClientList({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { clients, tokenView, setConfirm, busy, loading, conflict, reconciliationRequired } = m;
  return <>
      {clients.length > 0 && <ul className="record-list">{clients.map((client) => <li key={client.id} className="record-row flex flex-wrap justify-between gap-3"><div><strong>{client.name}</strong><p className="text-muted-foreground">{client.token_prefix} · {t(client.revoked_at ? 'revoked' : 'clientActive')}</p><p className="text-muted-foreground">{t('clientScopeSummary', { tools: client.bindings.map((binding) => binding.name).join(', '), sources: client.source_ids.length })}</p></div><div className="flex gap-2"><Button variant="outline" disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView) || Boolean(client.revoked_at)} onClick={() => setConfirm({ action: 'rotate', client })}>{t('rotate')}</Button><Button variant="outline" disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView) || Boolean(client.revoked_at)} onClick={() => setConfirm({ action: 'revoke', client })}>{t('revoke')}</Button></div></li>)}</ul>}
  </>;
}

/** Native tool fingerprints and client identities for review. */
function InboundTechnicalDetails({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { clients, tools } = m;
  return <>
      {(tools.length > 0 || clients.length > 0) && <details><summary className="cursor-pointer text-sm">{t('technicalDetails')}</summary><div className="space-y-1 text-muted-foreground">
        {tools.map((tool) => <p key={`${tool.name}@${tool.version}`}>{tool.name} · {tool.version}: <code>{tool.schema_fingerprint}</code></p>)}
        {clients.map((client) => <p key={client.id}>{client.name} · {client.audience} · {t('revision', { revision: client.revision })}</p>)}
      </div></details>}
  </>;
}

/** One-time token panel: copy and acknowledged dismissal; plaintext stays in volatile state. */
function OneTimeToken({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { tokenView, copied, tokenAcknowledged, setConfirm, busy, copyToken, markTokenAcknowledged } = m;
  return <>
      {tokenView && <aside className="rounded-md border border-border p-4" aria-live="polite"><h4>{t('oneTimeToken')}</h4><p className="text-muted-foreground">{t('tokenOnce', { name: tokenView.client.name, prefix: tokenView.client.token_prefix })}</p><Input readOnly type="text" autoComplete="off" spellCheck={false} value={tokenView.token} aria-label={t('oneTimeToken')} /><div className="flex gap-2"><Button variant="outline" disabled={busy} onClick={() => void copyToken()}>{t('copyToken')}</Button>{copied && <span role="status">{t('copied')}</span>}<Button variant="outline" disabled={busy} onClick={() => setConfirm({ action: 'dismiss', client: tokenView.client })}>{t('dismissToken')}</Button></div><CheckboxField><Checkbox id="mcp-token-ack" checked={tokenAcknowledged} onCheckedChange={(checked) => { markTokenAcknowledged(Boolean(checked)); }} /><Label htmlFor="mcp-token-ack">{t('copyAcknowledgement')}</Label></CheckboxField><p className="text-muted-foreground">{t('clipboardNotAcceptance')}</p></aside>}
  </>;
}

/** Collapsible inbound clients section composed of the issue form, list, details and token panel. */
function InboundClients({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { tokenView } = m;
  return <>
    <details className="group rounded-lg border border-border bg-card p-5" open={tokenView ? true : undefined}><summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-semibold [&::-webkit-details-marker]:hidden"><ChevronRight aria-hidden="true" className="size-4 transition-transform group-open:rotate-90" />{t('inboundClients')}</summary><section className="space-y-4 mt-3" aria-labelledby="mcp-inbound-title"><h3 id="mcp-inbound-title" className="sr-only">{t('inboundClients')}</h3><p className="text-muted-foreground">{t('inboundDescription')}</p>
      <InboundIssueForm m={m} />
      <InboundClientList m={m} />
      <InboundTechnicalDetails m={m} />
      <OneTimeToken m={m} />
    </section></details>
  </>;
}

/** Confirmation dialog for rotate/revoke/dismiss/save-empty-grants/discover actions. */
function ClientActionDialog({ m }: { m: McpModel }) {
  const t = useTranslations('mcp');
  const { tokenAcknowledged, confirm, setConfirm, confirmClientAction } = m;
  return <>
    <AlertDialog open={Boolean(confirm)} onOpenChange={(open) => { if (!open) setConfirm(null); }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t(CONFIRM_KEYS[confirm?.action ?? 'dismiss'].title)}</AlertDialogTitle><AlertDialogDescription>{t(CONFIRM_KEYS[confirm?.action ?? 'dismiss'].description)}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>{t('cancel')}</AlertDialogCancel><Button disabled={confirm?.action === 'dismiss' && !tokenAcknowledged} onClick={confirmClientAction}>{t('confirm')}</Button></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </>;
}

/**
 * Renders owner-scoped outbound connections, descriptor review, grants, and inbound clients.
 * Uses the authenticated workspace CSRF token for mutations; the server remains responsible
 * for authorization and permission checks. Draft identity is fixed to its original ID and
 * revision, async work is fenced across selection/session/unmount, and one-time tokens stay
 * in volatile component state until acknowledged dismissal or accepted session/page cleanup.
 */
export function McpSettings() {
  const m = useMcpSettings();
  const { selected, editing, draft } = m;
  return <section className="space-y-6" aria-labelledby="mcp-connections-title">
    <McpStatusBanners m={m} />
    <section className="flex flex-col gap-4" aria-labelledby="mcp-connections-title">
      <ConnectionList m={m} />
      {(selected || (editing && draft)) && <div className="space-y-3 border-t border-border pt-4">
        <SelectedConnectionHeader m={m} />
        <ConnectionEditorForm m={m} />
        <SavedGrants m={m} />
        <DescriptorReview m={m} />
        <ConnectionTechnicalDetails m={m} />
      </div>}
    </section>
    <InboundClients m={m} />
    <ClientActionDialog m={m} />
  </section>;
}
