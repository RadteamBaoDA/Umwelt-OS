'use client';

import { TablePagination } from '@/components/ui/pagination';
import Link from 'next/link';
import { Check, Link2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from '@/components/ui/alert-dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableRow } from '@/components/ui/table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ApiError } from '@/core/api';
import { CheckboxField, FieldGrid, SettingsActions, SettingsSection } from '@/components/settings-layout';
import { formatDateTime } from '@/core/i18n';
import { FollowEntityButton } from '@/modules/detail';
import { EntityGraph } from './entity-graph';
import { EventDetail } from '@/modules/timeline';
import type { EntityDetailViewModel } from './entity-detail-model';
import { conflictDetails } from './entity-detail-schemas';

/** Back link, entity heading with citation/AI actions, and revision/update error notices. */
function EntityHeader({ vm }: { vm: EntityDetailViewModel }) {
  const { t, display, copied, update, formatError, staleIdentity, clearIdentityDraft, copyCitation, value, baselineSnapshot, keepIdentityDraft } = vm;
  return (
<>
    <Link href="/knowledge/entities" className="inline-flex min-h-9 pointer-coarse:min-h-11 w-fit items-center text-sm font-semibold">← {t('title')}</Link>
    <header className="flex items-start gap-1"><div className="min-w-0 flex-1"><h1 className="mt-1 mb-1">{value.name ?? t('unnamedEntity')}</h1><p className="meta text-muted-foreground m-0">{t('revision')} {value.revision} · {value.first_seen_at ? `${t('firstSeen')} ${formatDateTime(value.first_seen_at, display.locale, display.timezone)}` : t('ownerManaged')}</p></div><Button type="button" variant="ghost" size="icon" aria-label={t('copyCitation')} title={t('copyCitation')} onClick={copyCitation}>{copied ? <Check className="h-[18px] w-[18px]" aria-hidden="true" /> : <Link2 className="h-[18px] w-[18px]" aria-hidden="true" />}</Button></header>
    {staleIdentity && <div className="rounded-lg border bg-card p-4 text-card-foreground" role="status"><p>{t('revisionChanged', { base: baselineSnapshot?.revision ?? 0, current: value.revision })}</p><Button type="button" variant="outline" onClick={keepIdentityDraft}>{t('keepDraft')}</Button><Button type="button" variant="outline" onClick={clearIdentityDraft}>{t('discardDraft')}</Button></div>}
    {update.error && <p className="error" role="alert">{formatError(update.error)}{update.error instanceof ApiError && update.error.status === 409 ? ` ${t('conflictReload')}` : ''}</p>}
    {conflictDetails(update.error).map((line, index) => <p className="error" key={index}>{line}</p>)}
</>
  );
}

/** Tab triggers for the entity detail panels, with evidence/event counts and the unconfirmed-alias badge. */
function EntityTabStrip({ vm }: { vm: EntityDetailViewModel }) {
  const { t, isAdminViewer, evidence, entityTimeline, evidenceItems, entityEvents, unconfirmedAliases, tabClass } = vm;
  return (
      <TabsList className="mt-2 h-auto w-full justify-start gap-0.5 overflow-x-auto rounded-none border-b border-border bg-transparent p-0">
        <TabsTrigger value="overview" className={tabClass}>{t('tabOverview')}</TabsTrigger>
        <TabsTrigger value="timeline" className={tabClass}>{t('tabTimeline')}</TabsTrigger>
        <TabsTrigger value="documents" className={tabClass}>{t('tabDocuments')}{evidenceItems.length > 0 ? ` ${evidenceItems.length}${evidence.hasNextPage ? '+' : ''}` : ''}</TabsTrigger>
        <TabsTrigger value="events" className={tabClass}>{t('tabEvents')}{entityEvents.length > 0 ? ` ${entityEvents.length}${entityTimeline.hasNextPage ? '+' : ''}` : ''}</TabsTrigger>
        {!isAdminViewer && <TabsTrigger value="review" className={tabClass}>{t('tabReview')}{unconfirmedAliases.length > 0 ? ` ${unconfirmedAliases.length}` : ''}</TabsTrigger>}
      </TabsList>
  );
}

/** Identity and alias editing sections of the Overview tab. */
function IdentityColumn({ vm }: { vm: EntityDetailViewModel }) {
  const { t, isAdminViewer, setTab, unconfirmedAliases } = vm;
  return (
        <div className="flex min-w-0 flex-col gap-4 min-[900px]:col-span-2">
          {!isAdminViewer && unconfirmedAliases.length > 0 && <div className="flex flex-col gap-2 rounded-lg border border-dashed border-border px-4 py-3" role="status"><h3 className="m-0 text-[15px] font-semibold">{t('needsReview')}</h3><span className="text-sm">{t('needsReviewBody', { count: unconfirmedAliases.length })}</span><Button type="button" variant="outline" className="self-start" onClick={() => setTab('review')}>{t('openReview')}</Button></div>}
      <IdentitySection vm={vm} />
      <AliasesSection vm={vm} />
        </div>
  );
}

/** Relationship table, graph and relationship evidence of the Overview tab. */
function RelationshipsColumn({ vm }: { vm: EntityDetailViewModel }) {
  const { t, display, selectedRelationshipId, setSelectedRelationshipId, neighbors, relationshipEvidence, neighborsPager, neighborsItems, relationshipRows, value } = vm;
  return (
        <div className="flex min-w-0 flex-col gap-3 min-[900px]:col-span-3"><h2 className="m-0 text-[15px] font-semibold">{t('relationships')}</h2>
    {neighbors.isError && <p className="error" role="alert">{t('relationshipUnavailable')} <Button variant="outline" onClick={() => neighbors.refetch()}>{t('retry')}</Button></p>}
    {neighborsItems.length > 0 && <><Table><TableBody>{neighborsPager.pageItems.map(({ entity: neighbor, relationship }) => <TableRow key={relationship.id}>
      <TableCell><Link className="font-semibold" href={`/knowledge/entities/${neighbor.id}`}>{neighbor.name ?? t(`type_${neighbor.type}` as 'type_person')}</Link><span className="meta block">{relationship.type}</span></TableCell>
      <TableCell><Badge variant="outline" className={relationship.origin === 'derived' ? 'border-dashed' : undefined}>{relationship.origin}</Badge></TableCell>
      <TableCell className="text-end"><Button type="button" variant="outline" aria-pressed={selectedRelationshipId === relationship.id} onClick={() => setSelectedRelationshipId(relationship.id)}>{t('selectRelationship')}</Button></TableCell>
    </TableRow>)}</TableBody></Table><TablePagination {...neighborsPager} /></>}
    {neighbors.hasNextPage && <Button variant="outline" disabled={neighbors.isFetchingNextPage} onClick={() => neighbors.fetchNextPage()}>{t('loadRelationships')}</Button>}
    {!neighborsItems.length && <p className="text-muted-foreground">{t('noRelationships')}</p>}
    <EntityGraph entityId={value.id} title={value.name ?? t('unnamedEntity')} selectedRelationshipId={selectedRelationshipId ?? undefined} onSelectRelationship={setSelectedRelationshipId} />
    <section aria-labelledby="relationship-evidence-heading"><h3 id="relationship-evidence-heading">{t('relationshipEvidence')}</h3>
      {!selectedRelationshipId && <p className="text-muted-foreground">{t('relationshipSelected')}</p>}
      {relationshipEvidence.isError && <p className="error" role="alert">{t('relationshipUnavailable')} <Button variant="outline" onClick={() => relationshipEvidence.refetch()}>{t('retry')}</Button></p>}
      {relationshipRows.map((item) => <article className="border-b border-border py-4 last:border-b-0" key={item.id}><p><Link href={`/knowledge/documents/${item.document_id}?version=${item.version_number}#cited-revision`}>{item.title} · {t('documentVersion')} {item.version_number}</Link> · {t('source')} {item.source_id}</p><small className="text-muted-foreground">{item.metadata_is_version_snapshot ? t('metadataVersionSnapshot') : t('metadataCurrentFallback')}</small><blockquote>{item.excerpt}</blockquote><small>{t('observed')} {formatDateTime(item.observed_at, display.locale, display.timezone)} · {t('confidence')} {Number.isFinite(item.confidence) ? item.confidence.toFixed(2) : t('unknownValue')}</small><p>{t('citationCount')}: 1 · {t('membership')} {item.source_entity_membership_id ?? t('unknownValue')} / {item.target_entity_membership_id ?? t('unknownValue')}</p></article>)}
      {relationshipEvidence.hasNextPage && <Button variant="outline" disabled={relationshipEvidence.isFetchingNextPage} onClick={() => relationshipEvidence.fetchNextPage()}>{t('loadEvidence')}</Button>}
    </section>
        <span className="meta">{t('inferredLegend')}</span>
        </div>
  );
}

/** Overview panel: identity column beside relationships column. */
function OverviewTab({ vm }: { vm: EntityDetailViewModel }) {
  return (
      <TabsContent value="overview" className="mt-5"><div className="grid grid-cols-1 gap-6 min-[900px]:grid-cols-5">
<IdentityColumn vm={vm} />
<RelationshipsColumn vm={vm} />
      </div></TabsContent>
  );
}

/** History panel with owner actions and retained evidence memberships. */
function TimelineTab({ vm }: { vm: EntityDetailViewModel }) {
  const { t, display, history, historyEvidence, historyRows, historyEvidenceRows } = vm;
  return (
      <TabsContent value="timeline" className="mt-5"><SettingsSection title={t('history')}>
    <Tabs defaultValue="owner-actions">
      <TabsList><TabsTrigger value="owner-actions">{t('ownerActions')}</TabsTrigger><TabsTrigger value="retained-evidence">{t('retainedEvidence')}</TabsTrigger></TabsList>
      <TabsContent value="owner-actions">
        <p className="text-muted-foreground">{t('historyValuesUnavailable')}</p>
        {history.isError && <p className="error" role="alert">{t('historyUnavailable')} <Button variant="outline" onClick={() => history.refetch()}>{t('retry')}</Button></p>}
        <ul className="flex flex-col gap-3">{historyRows.map((item) => <li className="border-b border-border py-4 last:border-b-0" key={item.id}><strong>{item.operation}</strong><p>{t('recorded')} {formatDateTime(item.recorded_at, display.locale, display.timezone)}</p><p>{t('affectedIds')}: {item.affected_ids.join(', ') || t('none')}</p><p>{t('revisions')}: {Object.entries(item.revisions).map(([id, revision]) => `${id}: ${revision}`).join(', ') || t('none')}</p></li>)}</ul>
        {history.hasNextPage && <Button variant="outline" disabled={history.isFetchingNextPage} onClick={() => history.fetchNextPage()}>{t('loadHistory')}</Button>}
      </TabsContent>
      <TabsContent value="retained-evidence">
        {historyEvidence.isError && <p className="error" role="alert">{t('historyUnavailable')} <Button variant="outline" onClick={() => historyEvidence.refetch()}>{t('retry')}</Button></p>}
        <ul className="flex flex-col gap-3">{historyEvidenceRows.map((item) => <li className="border-b border-border py-4 last:border-b-0" key={item.id}><Link href={`/knowledge/documents/${item.document_id}?version=${item.version_number}#cited-revision`}>{item.title} · {t('documentVersion')} {item.version_number}</Link><small className="text-muted-foreground">{item.metadata_is_version_snapshot ? t('metadataVersionSnapshot') : t('metadataCurrentFallback')}</small><p>{t('observed')} {formatDateTime(item.observed_at, display.locale, display.timezone)}</p><blockquote>{item.excerpt}</blockquote></li>)}</ul>
        {historyEvidence.hasNextPage && <Button variant="outline" disabled={historyEvidence.isFetchingNextPage} onClick={() => historyEvidence.fetchNextPage()}>{t('loadHistory')}</Button>}
      </TabsContent>
    </Tabs>
      </SettingsSection></TabsContent>
  );
}

/** Evidence panel with selection, stale-selection notice and the suppress confirmation flow. */
function DocumentsTab({ vm }: { vm: EntityDetailViewModel }) {
  const { t, display, selected, setSelected, selectionRevision, setSelectionRevision, evidence, evidenceItems, staleSelection, addSelectedEvidence, value } = vm;
  return (
      <TabsContent value="documents" className="mt-5"><SettingsSection title={t('evidence')}>
    {evidence.isError && <p className="error" role="alert">{t('evidenceUnavailable')} <Button variant="outline" onClick={() => evidence.refetch()}>{t('retry')}</Button></p>}
    <ul className="m-0 flex list-none flex-col gap-3 p-0">{evidenceItems.map((item) => <li className="border-b border-border py-4 last:border-b-0" key={item.id}>
      <CheckboxField><Checkbox id={`evidence-${item.id}`} checked={selected.includes(item.id)} onCheckedChange={(checked) => addSelectedEvidence(item.id, checked === true)} /><Label htmlFor={`evidence-${item.id}`}>{t('selectEvidence')}</Label></CheckboxField>
      <h3><Link href={`/knowledge/documents/${item.document_id}?version=${item.version_number}#cited-revision`}>{item.title}</Link></h3>
      <small className="text-muted-foreground">{item.metadata_is_version_snapshot ? t('metadataVersionSnapshot') : t('metadataCurrentFallback')}</small>
      <blockquote>{item.excerpt}</blockquote>
      <small className="text-muted-foreground">{t('observed')} {formatDateTime(item.observed_at, display.locale, display.timezone)} · {t('confidence')} {Number.isFinite(item.confidence) ? item.confidence.toFixed(2) : t('unknownValue')} · {t('membership')} {item.id}</small>
    </li>)}</ul>
    {evidence.hasNextPage && <Button variant="outline" disabled={evidence.isFetchingNextPage} onClick={() => evidence.fetchNextPage()}>{t('loadEvidence')}</Button>}
    {staleSelection && <p className="error" role="status">{t('selectionRevisionChanged', { base: selectionRevision ?? 0, current: value.revision })} <Button variant="outline" onClick={() => { setSelected([]); setSelectionRevision(null); }}>{t('clearSelection')}</Button></p>}
      <SuppressControls vm={vm} />
      </SettingsSection></TabsContent>
  );
}

/** Entity event timeline panel. */
function EventsTab({ vm }: { vm: EntityDetailViewModel }) {
  const { t, display, entityTimeline, entityEvents, entityGraphStatuses, value } = vm;
  return (
      <TabsContent value="events" className="mt-5"><SettingsSection title={t('entityTimeline')}>
      {entityTimeline.isError && <p className="error" role="alert">{t('timelineUnavailable')} <Button variant="outline" onClick={() => entityTimeline.refetch()}>{t('retry')}</Button></p>}
      {entityTimeline.data && <p className="text-muted-foreground" role="status">{t('graphStatuses')}: {entityGraphStatuses.map((status) => `${status.status}${status.graph_enabled ? '' : ` (${t('graphDisabled')})`}${status.error_code ? ` (${status.error_code})` : ''}`).join(', ') || t('noGraphStatuses')}</p>}
      <div className="flex flex-col gap-3">{entityEvents.map((event) => <EventDetail key={event.id} event={event} locale={display.locale} timezone={display.timezone} entityNames={new Map([[value.id, value.name ?? t('unnamedEntity')]])} />)}</div>
      {entityTimeline.hasNextPage && <Button variant="outline" disabled={entityTimeline.isFetchingNextPage} onClick={() => entityTimeline.fetchNextPage()}>{t('loadTimeline')}</Button>}
      </SettingsSection></TabsContent>
  );
}

/** Split-entity form and preview inside the Review tab. */
function SplitSection({ vm }: { vm: EntityDetailViewModel }) {
  const { t, selected, correction, splitPreview, split, currentSplitPreview, formatError, staleSelection, previewSplitForm, confirmSplitForm } = vm;
  return (
        <SettingsSection title={t('split')}><p className="text-muted-foreground m-0">{t('splitHelp')}</p>
    <div className="field"><Label htmlFor="split-name">{t('newEntityName')}</Label><Input id="split-name" maxLength={300} aria-invalid={!!correction.formState.errors.splitName} {...correction.register('splitName')} onChange={(event) => { correction.setValue('splitName', event.target.value, { shouldDirty: true }); correction.clearErrors('splitName'); }} />{correction.formState.errors.splitName && <p className="error" role="alert">{t('splitNameRequired')}</p>}</div>
    <Button variant="outline" disabled={!selected.length || staleSelection || splitPreview.isPending} onClick={previewSplitForm}>{t('previewSplit')}</Button>
    {splitPreview.error && <p className="error" role="alert">{t('splitPreviewFailed')} {formatError(splitPreview.error)}</p>}
    {currentSplitPreview && <div className="rounded-lg border bg-card p-4 text-card-foreground" role="status"><p>{t('correctionScope')}: {currentSplitPreview.preview.entity_ids.length} · {t('memberships')}: {currentSplitPreview.preview.membership_ids.length} · {t('relationshipCount')}: {currentSplitPreview.preview.relationship_ids.length} · {t('evidenceReferences')}: {currentSplitPreview.preview.evidence_ref_count}</p><p>{t('selectedMemberships')}: {currentSplitPreview.payload.evidence_ids.slice(0, 20).join(', ')}{currentSplitPreview.payload.evidence_ids.length > 20 ? ` · ${t('additionalItems', { count: currentSplitPreview.payload.evidence_ids.length - 20 })}` : ''}</p><p>{t('reason')}: {currentSplitPreview.payload.reason} · {t('revision')} {currentSplitPreview.payload.expected_revision}</p>{currentSplitPreview.preview.conflicts.map((conflict) => <p className="error" key={conflict.code}>{conflict.code}: {conflict.message} · {conflict.entity_ids.slice(0, 5).join(', ')} · {conflict.membership_ids.slice(0, 5).join(', ')} · {conflict.relationship_ids.slice(0, 5).join(', ')}</p>)}<AlertDialog><AlertDialogTrigger asChild><Button type="button" disabled={split.isPending || !!currentSplitPreview.preview.conflicts.length}>{t('confirmSplit')}</Button></AlertDialogTrigger><AlertDialogContent onEscapeKeyDown={(event) => { if (split.isPending) event.preventDefault(); }}><AlertDialogHeader><AlertDialogTitle>{t('confirmSplit')}</AlertDialogTitle><AlertDialogDescription>{t('splitConfirmationDescription')}</AlertDialogDescription></AlertDialogHeader><div className="flex flex-col gap-3"><p>{t('newEntityName')}: {currentSplitPreview.payload.new_entity.name} · {t(`type_${currentSplitPreview.payload.new_entity.type}` as 'type_person')}</p><p>{t('revision')}: {currentSplitPreview.payload.expected_revision} · {t('memberships')}: {currentSplitPreview.payload.evidence_ids.length} · {t('relationshipCount')}: {currentSplitPreview.preview.relationship_ids.length} · {t('evidenceReferences')}: {currentSplitPreview.preview.evidence_ref_count}</p><p>{t('reason')}: {currentSplitPreview.payload.reason}</p><p>{t('selectedMemberships')}: {currentSplitPreview.payload.evidence_ids.slice(0, 20).join(', ')}{currentSplitPreview.payload.evidence_ids.length > 20 ? ` · ${t('additionalItems', { count: currentSplitPreview.payload.evidence_ids.length - 20 })}` : ''}</p></div>{split.error && <div className="error" role="alert"><p>{t('correctionWriteFailed')} {formatError(split.error)}</p>{conflictDetails(split.error).map((line, index) => <p key={index}>{line}</p>)}</div>}<AlertDialogFooter><AlertDialogCancel disabled={split.isPending}>{t('cancel')}</AlertDialogCancel><Button disabled={split.isPending} onClick={confirmSplitForm}>{split.isPending ? t('saving') : t('confirmSplit')}</Button></AlertDialogFooter></AlertDialogContent></AlertDialog></div>}
    {splitPreview.data && !currentSplitPreview && <p className="error" role="status">{t('previewStale')}</p>}
        </SettingsSection>
  );
}

/** Merge-entity form and preview inside the Review tab. */
function MergeSection({ vm }: { vm: EntityDetailViewModel }) {
  const { t, selectedMergeTarget, correction, mergePreview, previewMergeForm } = vm;
  return (
        <SettingsSection title={t('merge')}><p className="text-muted-foreground m-0">{t('mergeHelp')}</p>
      <MergeTargetPicker vm={vm} />
    <div className="field"><Label htmlFor="correction-reason">{t('reason')}</Label><Input id="correction-reason" maxLength={300} aria-invalid={!!correction.formState.errors.reason} {...correction.register('reason')} />{correction.formState.errors.reason && <p className="error" role="alert">{t('correctionReasonRequired')}</p>}</div>
    <Button variant="outline" disabled={!selectedMergeTarget || mergePreview.isPending} onClick={previewMergeForm}>{t('previewMerge')}</Button>
      <MergePreviewPanel vm={vm} />
        </SettingsSection>
  );
}

/** Review panel (owner viewers only) with split and merge corrections. */
function ReviewTab({ vm }: { vm: EntityDetailViewModel }) {
  return (
      <TabsContent value="review" className="mt-5 flex flex-col gap-5">
<SplitSection vm={vm} />
<MergeSection vm={vm} />
      </TabsContent>
  );
}

/** Follow, correct-identity and ask-AI actions below the tabs. */
function EntityActionsBar({ vm }: { vm: EntityDetailViewModel }) {
  const { t, isAdminViewer, askAi, correctIdentity, value } = vm;
  return (
    <div className="mt-5 flex flex-wrap items-start gap-2 border-t border-border pt-4">{!isAdminViewer && <><FollowEntityButton entityId={value.id} name={value.name} /><Button type="button" variant="outline" onClick={correctIdentity}>{t('correctIdentity')}</Button><span className="flex-1" /><Button type="button" onClick={askAi}>{t('askAiAbout')}</Button></>}</div>
  );
}

/** Confirmation dialog that removes an alias with the captured reason. */
function AliasRemovalDialog({ vm }: { vm: EntityDetailViewModel }) {
  const { t, aliasRemovalSnapshot, setAliasRemovalSnapshot, removeAlias, formatError, confirmAliasRemoval } = vm;
  return (
    <AlertDialog open={!!aliasRemovalSnapshot} onOpenChange={(open) => { if (!open && !removeAlias.isPending) { setAliasRemovalSnapshot(null); removeAlias.reset(); } }}>
      <AlertDialogContent onEscapeKeyDown={(event) => { if (removeAlias.isPending) event.preventDefault(); }}>
        <AlertDialogHeader><AlertDialogTitle>{t('removeAliasConfirmation')}</AlertDialogTitle><AlertDialogDescription>{t('aliasRemovalDescription')}</AlertDialogDescription></AlertDialogHeader>
        {aliasRemovalSnapshot && <div className="flex flex-col gap-3"><p>{t('name')}: {aliasRemovalSnapshot.aliasName}</p><p>{t('reason')}: {aliasRemovalSnapshot.reason}</p></div>}
        {removeAlias.error && <p className="error" role="alert">{t('aliasFailed')} {formatError(removeAlias.error)}</p>}
        <AlertDialogFooter><AlertDialogCancel disabled={removeAlias.isPending}>{t('cancel')}</AlertDialogCancel><Button disabled={!aliasRemovalSnapshot || removeAlias.isPending} onClick={confirmAliasRemoval}>{removeAlias.isPending ? t('saving') : t('removeAlias')}</Button></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** Full entity detail screen once the entity is loaded: header, tabbed panels, action bar and alias-removal dialog. */
export function EntityDetailView({ vm }: { vm: EntityDetailViewModel }) {
  const { tab, setTab, isAdminViewer } = vm;
  return (
    <section className="rounded-lg border bg-card p-6">
      <EntityHeader vm={vm} />
      <Tabs value={tab} onValueChange={setTab}>
        <EntityTabStrip vm={vm} />
        <OverviewTab vm={vm} />
        <TimelineTab vm={vm} />
        <DocumentsTab vm={vm} />
        <EventsTab vm={vm} />
        {!isAdminViewer && <ReviewTab vm={vm} />}
      </Tabs>
      <EntityActionsBar vm={vm} />
      <AliasRemovalDialog vm={vm} />
    </section>
  );
}

/** Merge preview, stale notice and correction errors. */
function MergePreviewPanel({ vm }: { vm: EntityDetailViewModel }) {
  const { t, mergePreview, merge, currentMergePreview, correctionErrors, formatError, confirmMergeForm } = vm;
  return (
<>
    {mergePreview.error && <p className="error" role="alert">{t('mergePreviewFailed')} {formatError(mergePreview.error)}</p>}
    {currentMergePreview && <div className="rounded-lg border bg-card p-4 text-card-foreground" role="status"><p>{t('mergeInto')} {currentMergePreview.targetName} · {t('revision')} {currentMergePreview.intoRevision}</p><p>{t('sourceRevision')}: {currentMergePreview.sourceRevision} · {t('reason')}: {currentMergePreview.reason}</p><p>{t('entitiesCount')}: {currentMergePreview.preview.entity_ids.length} · {t('memberships')}: {currentMergePreview.preview.membership_ids.length} · {t('relationshipCount')}: {currentMergePreview.preview.relationship_ids.length} · {t('evidenceReferences')}: {currentMergePreview.preview.evidence_ref_count}</p><p>{t('selectedMemberships')}: {currentMergePreview.preview.membership_ids.slice(0, 20).join(', ')}{currentMergePreview.preview.membership_ids.length > 20 ? ` · ${t('additionalItems', { count: currentMergePreview.preview.membership_ids.length - 20 })}` : ''}</p>{currentMergePreview.preview.conflicts.map((conflict) => <p className="error" key={conflict.code}>{conflict.code}: {conflict.message} · {conflict.entity_ids.slice(0, 5).join(', ')} · {conflict.membership_ids.slice(0, 5).join(', ')} · {conflict.relationship_ids.slice(0, 5).join(', ')}</p>)}<AlertDialog><AlertDialogTrigger asChild><Button type="button" disabled={merge.isPending || !!currentMergePreview.preview.conflicts.length}>{t('confirmMerge')}</Button></AlertDialogTrigger><AlertDialogContent onEscapeKeyDown={(event) => { if (merge.isPending) event.preventDefault(); }}><AlertDialogHeader><AlertDialogTitle>{t('confirmMerge')}</AlertDialogTitle><AlertDialogDescription>{t('mergeConfirmationDescription')}</AlertDialogDescription></AlertDialogHeader><div className="flex flex-col gap-3"><p>{t('mergeInto')} {currentMergePreview.targetName} · {t('revision')} {currentMergePreview.intoRevision}</p><p>{t('sourceRevision')}: {currentMergePreview.sourceRevision} · {t('reason')}: {currentMergePreview.reason}</p><p>{t('entitiesCount')}: {currentMergePreview.preview.entity_ids.length} · {t('memberships')}: {currentMergePreview.preview.membership_ids.length} · {t('relationshipCount')}: {currentMergePreview.preview.relationship_ids.length} · {t('evidenceReferences')}: {currentMergePreview.preview.evidence_ref_count}</p><p>{t('selectedMemberships')}: {currentMergePreview.preview.membership_ids.slice(0, 20).join(', ')}{currentMergePreview.preview.membership_ids.length > 20 ? ` · ${t('additionalItems', { count: currentMergePreview.preview.membership_ids.length - 20 })}` : ''}</p></div>{merge.error && <div className="error" role="alert"><p>{t('correctionWriteFailed')} {formatError(merge.error)}</p>{conflictDetails(merge.error).map((line, index) => <p key={index}>{line}</p>)}</div>}<AlertDialogFooter><AlertDialogCancel disabled={merge.isPending}>{t('cancel')}</AlertDialogCancel><Button disabled={merge.isPending} onClick={confirmMergeForm}>{merge.isPending ? t('saving') : t('confirmMerge')}</Button></AlertDialogFooter></AlertDialogContent></AlertDialog></div>}
    {mergePreview.data && !currentMergePreview && <p className="error" role="status">{t('previewStale')}</p>}
    {correctionErrors.map((error, index) => <div key={index} className="error" role="alert">{formatError(error!)}{conflictDetails(error).map((line, i) => <p key={i}>{line}</p>)}</div>)}
</>
  );
}

/** Merge target search and selection fields. */
function MergeTargetPicker({ vm }: { vm: EntityDetailViewModel }) {
  const { t, targetSearch, setTargetSearch, selectedMergeTarget, setSelectedMergeTarget, correction, targets, targetItems, targetOptions, mergePreview } = vm;
  return (
<>
    <div className="field"><Label htmlFor="merge-target-search">{t('findMergeTarget')}</Label><Input id="merge-target-search" type="search" value={targetSearch} onChange={(event) => { setTargetSearch(event.target.value); setSelectedMergeTarget(null); correction.setValue('targetId', '', { shouldDirty: true }); correction.clearErrors('targetId'); mergePreview.reset(); }} /></div>
    {targetSearch.trim().length >= 2 && <div className="field"><Label htmlFor="merge-target">{t('mergeTarget')}</Label><Select value={selectedMergeTarget?.id ?? ''} onValueChange={(id) => { const chosen = targetItems.find((item) => item.id === id); if (!chosen) return; setSelectedMergeTarget(chosen); correction.setValue('targetId', chosen.id, { shouldDirty: true }); correction.clearErrors('targetId'); mergePreview.reset(); }}><SelectTrigger id="merge-target"><SelectValue placeholder={t('chooseMergeTarget')} /></SelectTrigger><SelectContent>{targetOptions.map((item) => <SelectItem key={item.id} value={item.id}>{item.name ?? t('unnamedEntity')} · {t(`type_${item.type}` as 'type_person')} · {t('revision')} {item.revision}</SelectItem>)}</SelectContent></Select>{selectedMergeTarget && <p className="text-muted-foreground">{t('selectedMergeTarget')}: {selectedMergeTarget.name ?? t('unnamedEntity')} · {t('revision')} {selectedMergeTarget.revision}</p>}{correction.formState.errors.targetId && <p className="error" role="alert">{t('mergeTargetRequired')}</p>}{targets.isError && <p className="error" role="alert">{targets.error.message} <Button type="button" variant="outline" onClick={() => targets.refetch()}>{t('retry')}</Button></p>}{targets.hasNextPage && <Button type="button" variant="outline" disabled={targets.isFetchingNextPage} onClick={() => targets.fetchNextPage()}>{t('loadMore')}</Button>}</div>}
</>
  );
}

/** Suppression confirmation checkbox, dialog and error notice. */
function SuppressControls({ vm }: { vm: EntityDetailViewModel }) {
  const { t, selected, selectionRevision, suppressConfirmed, setSuppressConfirmed, suppressSnapshot, setSuppressSnapshot, reason, suppress, formatError, staleSelection, suppressForm, value } = vm;
  return (
<>
    <CheckboxField><Checkbox id="entity-suppress-confirm" checked={suppressConfirmed} onCheckedChange={(checked) => setSuppressConfirmed(checked === true)} /><Label htmlFor="entity-suppress-confirm">{t('confirmSuppress', { count: selected.length })}</Label></CheckboxField>
    <AlertDialog open={!!suppressSnapshot} onOpenChange={(open) => { if (!open) setSuppressSnapshot(null); }}>
      <Button variant="outline" disabled={!selected.length || !suppressConfirmed || staleSelection || suppress.isPending || !reason.trim()} onClick={() => { if (selectionRevision !== null) setSuppressSnapshot({ evidenceIds: [...selected], revision: selectionRevision, reason: reason.trim() }); }}>{t('suppressEvidence')}</Button>
      <AlertDialogContent onEscapeKeyDown={(event) => { if (suppress.isPending) event.preventDefault(); }}>
        <AlertDialogHeader><AlertDialogTitle>{t('confirmSuppress', { count: suppressSnapshot?.evidenceIds.length ?? 0 })}</AlertDialogTitle><AlertDialogDescription>{t('suppressConfirmationDescription')}</AlertDialogDescription></AlertDialogHeader>
        {suppressSnapshot && <div className="flex flex-col gap-3"><p>{value.name ?? t('unnamedEntity')} · {t('revision')} {suppressSnapshot.revision}</p><p>{t('reason')}: {suppressSnapshot.reason}</p><p>{t('selectedMemberships')}: {suppressSnapshot.evidenceIds.slice(0, 20).join(', ')}{suppressSnapshot.evidenceIds.length > 20 ? ` · ${t('additionalItems', { count: suppressSnapshot.evidenceIds.length - 20 })}` : ''}</p></div>}
        {suppress.error && <p className="error" role="alert">{formatError(suppress.error)}</p>}
        <AlertDialogFooter><AlertDialogCancel disabled={suppress.isPending}>{t('cancel')}</AlertDialogCancel><Button disabled={suppress.isPending} onClick={suppressForm}>{suppress.isPending ? t('saving') : t('suppressEvidence')}</Button></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
    {suppress.error && <p className="error" role="alert">{formatError(suppress.error)}</p>}
</>
  );
}

/** Alias list and add-alias form. */
function AliasesSection({ vm }: { vm: EntityDetailViewModel }) {
  const { t, isAdminViewer, aliasForm, addAlias, removeAlias, formatError, openAliasRemoval, value } = vm;
  return (
          <SettingsSection title={t('aliases')}>
    {value.aliases.length ? <ul className="m-0 flex list-none flex-col gap-2 p-0">{value.aliases.map((item) => <li key={item.id} className="flex flex-wrap items-center gap-2"><Badge variant="outline" className={item.confirmed ? undefined : 'border-dashed'}>{item.alias} · {item.confirmed ? t('confirmed') : t('unconfirmed')}</Badge><span className="meta">{t('aliasOrigin')}: {item.origin}{item.confidence !== null && Number.isFinite(item.confidence) ? ` · ${t('confidence')} ${item.confidence.toFixed(2)}` : ` · ${t('confidence')} ${t('unknownValue')}`}{item.source_id && <span> · {t('source')} {item.source_id}</span>}</span> {!isAdminViewer && <Button type="button" variant="outline" disabled={removeAlias.isPending} onClick={() => openAliasRemoval(item.id, item.alias)}>{t('removeAlias')}</Button>}</li>)}</ul> : <p className="text-muted-foreground">{t('noAliases')}</p>}
    {addAlias.error && <p className="error" role="alert">{t('aliasFailed')} {formatError(addAlias.error)}</p>}
            {!isAdminViewer && <form className="flex flex-col gap-4" onSubmit={aliasForm.handleSubmit((values) => addAlias.mutate(values))}><FieldGrid><div className="field"><Label htmlFor="entity-alias">{t('addAlias')}</Label><Input id="entity-alias" maxLength={300} {...aliasForm.register('alias')} />{aliasForm.formState.errors.alias && <p className="error">{t('aliasRequired')}</p>}</div><div className="field"><Label htmlFor="alias-reason">{t('reason')}</Label><Input id="alias-reason" maxLength={300} {...aliasForm.register('reason')} /></div></FieldGrid><SettingsActions><Button disabled={addAlias.isPending}>{t('addAlias')}</Button></SettingsActions></form>}
          </SettingsSection>
  );
}

/** Identity name/description editing section. */
function IdentitySection({ vm }: { vm: EntityDetailViewModel }) {
  const { t, isAdminViewer, identity, update, value } = vm;
  return (
          <SettingsSection title={t('identity')}>
            <p className="text-muted-foreground m-0">{value.name_origin === 'owner' ? t('ownerIdentity') : value.name_origin ? t('derivedIdentity') : t('unknownOrigin')} · {t('nameOrigin')}: {value.name_origin ?? t('unknownOrigin')}</p>
            <p className="text-muted-foreground m-0">{t('descriptionOrigin')}: {value.description_origin ?? t('unknownOrigin')}</p>
            {!isAdminViewer && <form className="flex flex-col gap-4" onSubmit={identity.handleSubmit((values) => update.mutate(values))}>
              <FieldGrid>
                <div className="field"><Label htmlFor="entity-name">{t('name')}</Label><Input id="entity-name" maxLength={300} {...identity.register('name')} /><small>{value.name_origin === 'owner' ? t('ownerFieldHelp') : t('derivedFieldHelp')}</small></div>
                <div className="field"><Label htmlFor="entity-description">{t('description')}</Label><Input id="entity-description" maxLength={10_000} {...identity.register('description')} /><small>{value.description_origin === 'owner' ? t('ownerFieldHelp') : t('derivedFieldHelp')}</small></div>
              </FieldGrid>
              <div className="field"><Label htmlFor="entity-reason">{t('reason')}</Label><Input id="entity-reason" maxLength={300} {...identity.register('reason')} />{identity.formState.errors.reason && <p className="error">{t('reasonRequired')}</p>}</div>
              <SettingsActions><Button disabled={update.isPending || !identity.formState.isDirty || (!identity.formState.dirtyFields.name && !identity.formState.dirtyFields.description)}>{update.isPending ? t('saving') : t('saveIdentity')}</Button></SettingsActions>
            </form>}
          </SettingsSection>
  );
}
