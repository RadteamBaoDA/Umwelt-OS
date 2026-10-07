'use client';

import Link from 'next/link';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams, useRouter } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from '@/components/ui/alert-dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { addEntityAlias, deleteEntityAlias, entityKeys, getEntity, getEntityNeighbors, getEntityTimeline, getRelationshipEvidence, listEntities, listEntityEvidence, listEntityHistory, mergeEntity, previewMergeEntity, previewSplitEntity, splitEntity, suppressEntityEvidence, updateEntity, type Entity } from './api';
import { FollowEntityButton } from '@/modules/detail/follow-entity';
import { EntityGraph } from './entity-graph';
import { EventDetail } from '@/modules/timeline/event-detail';

const identitySchema = z.object({ name: z.string().max(300), description: z.string().max(10_000), reason: z.string().trim().min(1).max(300) });
type IdentityValues = z.infer<typeof identitySchema>;
const aliasSchema = z.object({ alias: z.string().trim().min(1).max(300), reason: z.string().trim().min(1).max(300) });
const correctionSchema = z.object({ targetId: z.string(), splitName: z.string(), reason: z.string().trim().min(1, 'correctionReasonRequired').max(300, 'correctionReasonRequired') });
const mergeConfirmationSchema = z.object({ targetId: z.string().trim().min(1).max(100), reason: z.string().trim().min(1).max(300) });
const splitConfirmationSchema = z.object({ splitName: z.string().trim().min(1).max(300), reason: z.string().trim().min(1).max(300) });
type CorrectionValues = z.infer<typeof correctionSchema>;

/** Extracts conflict details suitable for display from a request error. */
function conflictDetails(error: unknown): string[] {
  if (!(error instanceof ApiError) || error.status !== 409 || !error.details || typeof error.details !== 'object') return [];
  const details = error.details as Record<string, unknown>;
  /** Accepts only string conflict fields and truncates them to the caller’s display bound. */
  const boundedText = (value: unknown, limit: number) => typeof value === 'string' ? value.slice(0, limit) : null;
  const raw = Array.isArray(details.conflicts) ? details.conflicts : details.conflict ? [details.conflict] : [];
  return raw.slice(0, 5).flatMap((value) => {
    if (!value || typeof value !== 'object') return [];
    const conflict = value as Record<string, unknown>;
    return [
      [boundedText(conflict.code, 80), boundedText(conflict.message, 300)].filter((part): part is string => part !== null).join(': '),
      ...(['entity_ids', 'membership_ids', 'relationship_ids'] as const).flatMap((key) => {
        const ids = conflict[key];
        return Array.isArray(ids) && ids.length ? [`${key}: ${ids.slice(0, 5).map((id) => boundedText(id, 80)).filter((id): id is string => id !== null).join(', ')}`] : [];
      }),
    ].filter(Boolean).slice(0, 4);
  }).slice(0, 20);
}

/** Loads an entity and its evidence, then coordinates revision-fenced identity corrections. */
export function EntityDetail() {
  const t = useTranslations('entities');
  const { entityId } = useParams<{ entityId: string }>();
  const router = useRouter();
  const session = useWorkspaceSession();
  const display = useDisplayPreferences();
  const client = useQueryClient();
  const { registerLeaveGuard, ensureEntityDocument } = useGuardedNavigation();
  const [documentReadyId, setDocumentReadyId] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [selectionRevision, setSelectionRevision] = useState<number | null>(null);
  const [selectedRelationshipId, setSelectedRelationshipId] = useState<string | null>(null);
  const [targetSearch, setTargetSearch] = useState('');
  const [selectedMergeTarget, setSelectedMergeTarget] = useState<Entity | null>(null);
  const [suppressConfirmed, setSuppressConfirmed] = useState(false);
  const [suppressSnapshot, setSuppressSnapshot] = useState<{ evidenceIds: string[]; revision: number; reason: string } | null>(null);
  const [aliasRemovalSnapshot, setAliasRemovalSnapshot] = useState<{ aliasId: string; aliasName: string; reason: string } | null>(null);
  const baseline = useRef<{ id: string; revision: number; name: string; description: string } | null>(null);
  const identity = useForm<IdentityValues>({ resolver: zodResolver(identitySchema), defaultValues: { name: '', description: '', reason: 'owner_review' } });
  const aliasForm = useForm<z.infer<typeof aliasSchema>>({ resolver: zodResolver(aliasSchema), defaultValues: { alias: '', reason: 'owner_review' } });
  const correction = useForm<CorrectionValues>({ resolver: zodResolver(correctionSchema), mode: 'onChange', defaultValues: { targetId: '', splitName: '', reason: 'owner_review' } });
  const identityDirty = identity.formState.isDirty;
  const aliasDirty = aliasForm.formState.isDirty;
  const correctionDirty = correction.formState.isDirty;
  const dirtyRef = useRef(false);
  dirtyRef.current = identityDirty || aliasDirty || correctionDirty || selected.length > 0 || suppressConfirmed;

  const entity = useQuery({ queryKey: entityKeys.detail(entityId), queryFn: () => getEntity(entityId) });
  useEffect(() => { if (ensureEntityDocument()) setDocumentReadyId(entityId); }, [ensureEntityDocument, entityId]);
  const evidence = useInfiniteQuery({ queryKey: entityKeys.evidence(entityId), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntityEvidence(entityId, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const history = useInfiniteQuery({ queryKey: entityKeys.history(entityId), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntityHistory(entityId, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const historyEvidence = useInfiniteQuery({ queryKey: [...entityKeys.history(entityId), 'memberships'], initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntityHistory(entityId, undefined, pageParam), getNextPageParam: (page) => page.membership_next_cursor ?? undefined });
  const entityTimeline = useInfiniteQuery({ queryKey: entityKeys.timeline(entityId, { timezone: display.timezone }), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => getEntityTimeline(entityId, { timezone: display.timezone }, pageParam), getNextPageParam: (page) => page.timeline.next_cursor ?? undefined });
  const neighbors = useInfiniteQuery({ queryKey: entityKeys.neighbors(entityId), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => getEntityNeighbors(entityId, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const relationshipEvidence = useInfiniteQuery({ queryKey: ['relationships', selectedRelationshipId, 'evidence'], enabled: !!selectedRelationshipId, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => getRelationshipEvidence(selectedRelationshipId!, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const targetId = correction.watch('targetId');
  const splitName = correction.watch('splitName');
  const reason = correction.watch('reason');
  const targets = useInfiniteQuery({ queryKey: entityKeys.list(entity.data?.type, targetSearch), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntities(pageParam, entity.data?.type, targetSearch.trim()), getNextPageParam: (page) => page.next_cursor ?? undefined, enabled: !!entity.data && targetSearch.trim().length >= 2 });
  const targetItems = targets.data?.pages.flatMap((page) => page.items).filter((item) => item.id !== entity.data?.id) ?? [];
  const targetOptions = targetItems.map((item) => selectedMergeTarget?.id === item.id ? selectedMergeTarget : item);
  /** Invalidates entity and relationship queries after a successful correction. */
  const saved = () => { void client.invalidateQueries({ queryKey: entityKeys.all }); void client.invalidateQueries({ queryKey: ['relationships'] }); };

  useEffect(() => {
    const value = entity.data;
    if (!value || identity.formState.isDirty) return;
    baseline.current = { id: value.id, revision: value.revision, name: value.name ?? '', description: value.description ?? '' };
    identity.reset({ name: value.name ?? '', description: value.description ?? '', reason: 'owner_review' });
  }, [entity.data, identity.formState.isDirty, identity.reset]);
  /** Asks before discarding the current unsaved draft. */
  const confirmDiscard = useCallback(() => window.confirm(t('discardChangesQuestion')), [t]);
  /** Invalidates the current draft session and accepts leaving the editor. */
  const acceptLeave = useCallback(() => {
    dirtyRef.current = false;
    baseline.current = null;
    identity.reset({ name: '', description: '', reason: 'owner_review' });
    aliasForm.reset({ alias: '', reason: 'owner_review' });
    correction.reset({ targetId: '', splitName: '', reason: 'owner_review' });
    setSelected([]);
    setTargetSearch('');
    setSelectedMergeTarget(null);
    setSelectionRevision(null);
    setSuppressConfirmed(false);
  }, [identity.reset, aliasForm.reset, correction.reset]);
  useEffect(() => registerLeaveGuard({
    hasUnsavedChanges: () => dirtyRef.current,
    confirmDiscard,
    acceptLeave,
  }), [registerLeaveGuard, confirmDiscard, acceptLeave]);
  useEffect(() => { setSuppressConfirmed(false); setSuppressSnapshot(null); }, [selected, selectionRevision, entity.data?.revision, reason]);

  const update = useMutation({
    mutationFn: (values: IdentityValues) => {
      const original = baseline.current;
      if (!original) throw new Error(t('reloadReview'));
      const payload: { expected_revision: number; name?: string; description?: string | null; reason: string } = { expected_revision: original.revision, reason: values.reason };
      if (identity.formState.dirtyFields.name) payload.name = values.name.trim();
      if (identity.formState.dirtyFields.description) payload.description = values.description.trim() || null;
      if (!identity.formState.dirtyFields.name && !identity.formState.dirtyFields.description) throw new Error(t('saveIdentity'));
      return updateEntity(original.id, payload, session.csrfToken);
    },
    onSuccess: (value) => { baseline.current = { id: value.id, revision: value.revision, name: value.name ?? '', description: value.description ?? '' }; identity.reset({ name: value.name ?? '', description: value.description ?? '', reason: 'owner_review' }); client.setQueryData(entityKeys.detail(entityId), value); saved(); },
  });
  const addAlias = useMutation({ mutationFn: (values: z.infer<typeof aliasSchema>) => addEntityAlias(entity.data!.id, { alias: values.alias, confirmed: true, reason: values.reason }, session.csrfToken), onSuccess: (value) => { client.setQueryData(entityKeys.detail(entityId), value); aliasForm.reset({ alias: '', reason: 'owner_review' }); saved(); } });
  const removeAlias = useMutation({ mutationFn: (input: { aliasId: string; reason: string }) => deleteEntityAlias(entity.data!.id, input.aliasId, input.reason, session.csrfToken), onSuccess: () => { setAliasRemovalSnapshot(null); void client.invalidateQueries({ queryKey: entityKeys.detail(entityId) }); saved(); } });
  const mergePreview = useMutation({
    mutationFn: async (values: CorrectionValues) => {
      const source = entity.data!;
      const destination = selectedMergeTarget;
      if (!destination || destination.id === source.id) throw new Error(t('chooseMergeTarget'));
      if (destination.type !== source.type) throw new Error(t('mergeTargetTypeChanged'));
      if (values.targetId !== destination.id) throw new Error(t('chooseMergeTarget'));
      const preview = await previewMergeEntity(source.id, { into_id: destination.id, expected_revision: source.revision, expected_into_revision: destination.revision, reason: values.reason });
      return { preview, intoId: destination.id, intoRevision: destination.revision, targetName: destination.name ?? t('unnamedEntity'), sourceRevision: source.revision, reason: values.reason };
    },
  });
  useEffect(() => {
    setTargetSearch('');
    setSelectedMergeTarget(null);
    correction.reset({ targetId: '', splitName: '', reason: 'owner_review' });
    mergePreview.reset();
  }, [entityId, correction.reset, mergePreview.reset]);
  const merge = useMutation({
    mutationFn: () => { const preview = mergePreview.data!; return mergeEntity(entity.data!.id, { into_id: preview.intoId, expected_revision: preview.sourceRevision, expected_into_revision: preview.intoRevision, reason: preview.reason }, session.csrfToken); },
    onSuccess: (value) => { saved(); setSelected([]); setSelectionRevision(null); mergePreview.reset(); correction.reset(); setTargetSearch(''); setSelectedMergeTarget(null); router.replace(`/knowledge/entities/${value.canonical_entity_id}`); },
  });
  const suppress = useMutation({
    mutationFn: (snapshot: { evidence_ids: string[]; expected_revision: number; reason: string }) => suppressEntityEvidence(entity.data!.id, snapshot, session.csrfToken),
    onSuccess: () => { setSelected([]); setSelectionRevision(null); setSuppressSnapshot(null); saved(); void evidence.refetch(); },
  });
  /** Builds the split request from selected evidence, the selection revision, and validated form values. */
  const splitPayload = (values: CorrectionValues) => ({ evidence_ids: selected, expected_revision: selectionRevision ?? 0, new_entity: { type: entity.data!.type, name: values.splitName, reason: values.reason }, reason: values.reason });
  const splitPreview = useMutation({ mutationFn: async (values: CorrectionValues) => { if (selectionRevision === null) throw new Error(t('selectEvidence')); const payload = splitPayload(values); const preview = await previewSplitEntity(entity.data!.id, payload); return { preview, payload }; } });
  const split = useMutation({
    mutationFn: () => splitEntity(entity.data!.id, splitPreview.data!.payload, session.csrfToken),
    onSuccess: (value) => { saved(); const nextId = value.replacement_entity_ids[0] ?? value.canonical_entity_id; setSelected([]); setSelectionRevision(null); splitPreview.reset(); correction.reset(); setTargetSearch(''); setSelectedMergeTarget(null); router.replace(`/knowledge/entities/${nextId}`); },
  });

  if (documentReadyId !== entityId || entity.isPending) return <section className="content-panel"><div className="skeleton" aria-label={t('loadingEntity')} /></section>;
  if (entity.isError || !entity.data) return <section className="content-panel"><p className="error" role="alert">{t('unavailable')} <Button className="secondary" onClick={() => entity.refetch()}>{t('retry')}</Button></p></section>;
  const value = entity.data;
  if (value.id !== entityId) return <section className="content-panel"><h1>{t('canonicalEntityChanged')}</h1><p role="status">{t('canonicalEntityDetails', { id: value.id, revision: value.revision })}</p><Button onClick={() => router.replace(`/knowledge/entities/${value.id}`)}>{t('reviewCanonicalEntity')}</Button><Link href="/knowledge/entities">{t('cancel')}</Link></section>;
  const evidenceItems = evidence.data?.pages.flatMap((page) => page.items) ?? [];
  const neighborsItems = neighbors.data?.pages.flatMap((page) => page.items) ?? [];
  const currentMergePreview = mergePreview.data && mergePreview.data.sourceRevision === value.revision && mergePreview.data.intoId === selectedMergeTarget?.id && mergePreview.data.reason === reason.trim() && mergePreview.data.intoRevision === selectedMergeTarget?.revision ? mergePreview.data : null;
  const currentSplitPreview = splitPreview.data && splitPreview.data.payload.expected_revision === selectionRevision && splitPreview.data.payload.new_entity.name === splitName.trim() && splitPreview.data.payload.reason === reason.trim() && splitPreview.data.payload.evidence_ids.length === selected.length && splitPreview.data.payload.evidence_ids.every((id) => selected.includes(id)) ? splitPreview.data : null;
  const correctionErrors = [merge.error, mergePreview.error, split.error, splitPreview.error, suppress.error].filter(Boolean);
  /** Maps a document or entity API error to the feature’s displayable error state. */
  const formatError = (error: Error) => (error instanceof ApiError ? `${error.message}${error.code ? ` (${error.code})` : ''}` : error.message).slice(0, 500);
  const staleIdentity = !!baseline.current && (baseline.current.revision !== value.revision || baseline.current.id !== value.id);
  const staleSelection = selected.length > 0 && selectionRevision !== value.revision;
  const relationshipRows = relationshipEvidence.data?.pages.flatMap((page) => page.items) ?? [];
  const historyRows = history.data?.pages.flatMap((page) => page.items) ?? [];
  const historyEvidenceRows = historyEvidence.data?.pages.flatMap((page) => page.memberships) ?? [];
  const entityEvents = entityTimeline.data?.pages.flatMap((page) => page.timeline.items) ?? [];
  const entityGraphStatuses = entityTimeline.data?.pages.flatMap((page) => page.graph_statuses) ?? [];
  /** Clears pending identity edits without committing them. */
  const clearIdentityDraft = () => { baseline.current = { id: value.id, revision: value.revision, name: value.name ?? '', description: value.description ?? '' }; identity.reset({ name: value.name ?? '', description: value.description ?? '', reason: 'owner_review' }); update.reset(); };
  /** Stores the latest correction preview or submission issue for display. */
  const setCorrectionIssue = (field: 'targetId' | 'splitName' | 'reason', message: string) => correction.setError(field, { type: 'manual', message });
  /** Validates the merge target and requests a preview for the current entity revisions. */
  const previewMergeForm = correction.handleSubmit((values) => {
    const parsed = mergeConfirmationSchema.safeParse(values);
    if (!parsed.success) { setCorrectionIssue('targetId', 'mergeTargetRequired'); return; }
    if (!selectedMergeTarget || selectedMergeTarget.id !== parsed.data.targetId || selectedMergeTarget.type !== value.type) {
      setCorrectionIssue('targetId', 'mergeTargetRequired');
      return;
    }
    mergePreview.mutate({ ...values, ...parsed.data });
  });
  /** Confirms a still-current merge preview and submits the merge mutation. */
  const confirmMergeForm = correction.handleSubmit((values) => {
    const parsed = mergeConfirmationSchema.safeParse(values);
    if (!parsed.success || !currentMergePreview || !selectedMergeTarget || selectedMergeTarget.id !== parsed.data.targetId) {
      if (!parsed.success) setCorrectionIssue('targetId', 'mergeTargetRequired');
      return;
    }
    merge.mutate();
  });
  /** Validates the split form and previews the selected evidence against its captured revision. */
  const previewSplitForm = correction.handleSubmit((values) => {
    const parsed = splitConfirmationSchema.safeParse(values);
    if (!parsed.success) { setCorrectionIssue('splitName', 'splitNameRequired'); return; }
    if (!selected.length || staleSelection) return;
    splitPreview.mutate({ ...values, ...parsed.data });
  });
  /** Confirms a split preview only while its name and reason still match the validated form. */
  const confirmSplitForm = correction.handleSubmit((values) => {
    const parsed = splitConfirmationSchema.safeParse(values);
    if (!parsed.success) { setCorrectionIssue('splitName', 'splitNameRequired'); return; }
    if (!currentSplitPreview || parsed.data.reason !== currentSplitPreview.payload.reason || parsed.data.splitName !== currentSplitPreview.payload.new_entity.name) return;
    split.mutate();
  });
  /** Opens the confirmation state for removing the selected alias. */
  const openAliasRemoval = (aliasId: string, aliasName: string) => {
    void correction.handleSubmit((values) => {
      removeAlias.reset();
      setAliasRemovalSnapshot({ aliasId, aliasName, reason: values.reason.trim() });
    })();
  };
  /** Submits the confirmed alias removal and refreshes entity state on success. */
  const confirmAliasRemoval = () => {
    if (!aliasRemovalSnapshot || removeAlias.isPending) return;
    removeAlias.mutate({ aliasId: aliasRemovalSnapshot.aliasId, reason: aliasRemovalSnapshot.reason });
  };
  /** Submits evidence suppression only when the confirmed snapshot still matches the current selection. */
  const suppressForm = correction.handleSubmit((values) => {
    const snapshot = suppressSnapshot;
    if (!snapshot || !suppressConfirmed || staleSelection || snapshot.revision !== selectionRevision
      || snapshot.evidenceIds.length !== selected.length || !snapshot.evidenceIds.every((id) => selected.includes(id))
      || snapshot.reason !== values.reason.trim()) return;
    suppress.mutate({ evidence_ids: snapshot.evidenceIds, expected_revision: snapshot.revision, reason: snapshot.reason });
  });
  /** Adds the selected evidence IDs to the current correction draft. */
  const addSelectedEvidence = (id: string, checked: boolean) => {
    setSelected((current) => checked ? [...new Set([...current, id])] : current.filter((item) => item !== id));
    if (checked && selectionRevision === null) setSelectionRevision(value.revision);
    if (!checked && selected.length <= 1) setSelectionRevision(null);
  };

  return <section className="content-panel">
    <Link href="/knowledge/entities">← {t('title')}</Link>
    <h1>{value.name ?? t('unnamedEntity')}</h1>
    <FollowEntityButton entityId={value.id} name={value.name} />
    <p className="muted">{t(`type_${value.type}` as 'type_person')} · {t('revision')} {value.revision} · {value.first_seen_at ? `${t('firstSeen')} ${formatDateTime(value.first_seen_at, display.locale, display.timezone)}` : t('ownerManaged')}</p>
    {staleIdentity && <div className="card" role="status"><p>{t('revisionChanged', { base: baseline.current?.revision ?? 0, current: value.revision })}</p><Button type="button" className="secondary" onClick={() => { baseline.current = { id: value.id, revision: value.revision, name: value.name ?? '', description: value.description ?? '' }; }}>{t('keepDraft')}</Button><Button type="button" className="secondary" onClick={clearIdentityDraft}>{t('discardDraft')}</Button></div>}
    {update.error && <p className="error" role="alert">{formatError(update.error)}{update.error instanceof ApiError && update.error.status === 409 ? ` ${t('conflictReload')}` : ''}</p>}
    {conflictDetails(update.error).map((line, index) => <p className="error" key={index}>{line}</p>)}
    <h2>{t('identity')}</h2>
    <p className="muted">{value.name_origin === 'owner' ? t('ownerIdentity') : value.name_origin ? t('derivedIdentity') : t('unknownOrigin')} · {t('nameOrigin')}: {value.name_origin ?? t('unknownOrigin')}</p>
    <p className="muted">{t('descriptionOrigin')}: {value.description_origin ?? t('unknownOrigin')}</p>
    <form className="form" onSubmit={identity.handleSubmit((values) => update.mutate(values))}>
      <div className="field"><Label htmlFor="entity-name">{t('name')}</Label><Input id="entity-name" maxLength={300} {...identity.register('name')} /><small>{value.name_origin === 'owner' ? t('ownerFieldHelp') : t('derivedFieldHelp')}</small></div>
      <div className="field"><Label htmlFor="entity-description">{t('description')}</Label><Input id="entity-description" maxLength={10_000} {...identity.register('description')} /><small>{value.description_origin === 'owner' ? t('ownerFieldHelp') : t('derivedFieldHelp')}</small></div>
      <div className="field"><Label htmlFor="entity-reason">{t('reason')}</Label><Input id="entity-reason" maxLength={300} {...identity.register('reason')} />{identity.formState.errors.reason && <p className="error">{t('reasonRequired')}</p>}</div>
      <Button disabled={update.isPending || !identity.formState.isDirty || (!identity.formState.dirtyFields.name && !identity.formState.dirtyFields.description)}>{update.isPending ? t('saving') : t('saveIdentity')}</Button>
    </form>

    <h2>{t('aliases')}</h2>
    {value.aliases.length ? <ul className="stack">{value.aliases.map((item) => <li key={item.id}>{item.alias} · {item.confirmed ? t('confirmed') : t('unconfirmed')} · {t('aliasOrigin')}: {item.origin}{item.confidence !== null && Number.isFinite(item.confidence) ? ` · ${t('confidence')} ${item.confidence.toFixed(2)}` : ` · ${t('confidence')} ${t('unknownValue')}`}{item.source_id && <span> · {t('source')} {item.source_id}</span>} <Button type="button" className="secondary" disabled={removeAlias.isPending} onClick={() => openAliasRemoval(item.id, item.alias)}>{t('removeAlias')}</Button></li>)}</ul> : <p className="muted">{t('noAliases')}</p>}
    {addAlias.error && <p className="error" role="alert">{t('aliasFailed')} {formatError(addAlias.error)}</p>}
    <form className="form" onSubmit={aliasForm.handleSubmit((values) => addAlias.mutate(values))}><div className="field"><Label htmlFor="entity-alias">{t('addAlias')}</Label><Input id="entity-alias" maxLength={300} {...aliasForm.register('alias')} />{aliasForm.formState.errors.alias && <p className="error">{t('aliasRequired')}</p>}</div><div className="field"><Label htmlFor="alias-reason">{t('reason')}</Label><Input id="alias-reason" maxLength={300} {...aliasForm.register('reason')} /></div><Button disabled={addAlias.isPending}>{t('addAlias')}</Button></form>

    <h2>{t('evidence')}</h2>
    {evidence.isError && <p className="error" role="alert">{t('evidenceUnavailable')} <Button className="secondary" onClick={() => evidence.refetch()}>{t('retry')}</Button></p>}
    <ul className="stack">{evidenceItems.map((item) => <li className="card" key={item.id}>
      <label className="field"><span>{t('selectEvidence')}</span><Checkbox checked={selected.includes(item.id)} onCheckedChange={(checked) => addSelectedEvidence(item.id, checked === true)} /></label>
      <h3><Link href={`/knowledge/documents/${item.document_id}?version=${item.version_number}#cited-revision`}>{item.title}</Link></h3>
      <small className="muted">{item.metadata_is_version_snapshot ? t('metadataVersionSnapshot') : t('metadataCurrentFallback')}</small>
      <blockquote>{item.excerpt}</blockquote>
      <small className="muted">{t('observed')} {formatDateTime(item.observed_at, display.locale, display.timezone)} · {t('confidence')} {Number.isFinite(item.confidence) ? item.confidence.toFixed(2) : t('unknownValue')} · {t('membership')} {item.id}</small>
    </li>)}</ul>
    {evidence.hasNextPage && <Button className="secondary" disabled={evidence.isFetchingNextPage} onClick={() => evidence.fetchNextPage()}>{t('loadEvidence')}</Button>}
    {staleSelection && <p className="error" role="status">{t('selectionRevisionChanged', { base: selectionRevision ?? 0, current: value.revision })} <Button className="secondary" onClick={() => { setSelected([]); setSelectionRevision(null); }}>{t('clearSelection')}</Button></p>}
    <label className="field"><span>{t('confirmSuppress', { count: selected.length })}</span><Checkbox checked={suppressConfirmed} onCheckedChange={(checked) => setSuppressConfirmed(checked === true)} /></label>
    <AlertDialog open={!!suppressSnapshot} onOpenChange={(open) => { if (!open) setSuppressSnapshot(null); }}>
      <Button className="secondary" disabled={!selected.length || !suppressConfirmed || staleSelection || suppress.isPending || !reason.trim()} onClick={() => { if (selectionRevision !== null) setSuppressSnapshot({ evidenceIds: [...selected], revision: selectionRevision, reason: reason.trim() }); }}>{t('suppressEvidence')}</Button>
      <AlertDialogContent onEscapeKeyDown={(event) => { if (suppress.isPending) event.preventDefault(); }}>
        <AlertDialogHeader><AlertDialogTitle>{t('confirmSuppress', { count: suppressSnapshot?.evidenceIds.length ?? 0 })}</AlertDialogTitle><AlertDialogDescription>{t('suppressConfirmationDescription')}</AlertDialogDescription></AlertDialogHeader>
        {suppressSnapshot && <div className="stack"><p>{value.name ?? t('unnamedEntity')} · {t('revision')} {suppressSnapshot.revision}</p><p>{t('reason')}: {suppressSnapshot.reason}</p><p>{t('selectedMemberships')}: {suppressSnapshot.evidenceIds.slice(0, 20).join(', ')}{suppressSnapshot.evidenceIds.length > 20 ? ` · ${t('additionalItems', { count: suppressSnapshot.evidenceIds.length - 20 })}` : ''}</p></div>}
        {suppress.error && <p className="error" role="alert">{formatError(suppress.error)}</p>}
        <AlertDialogFooter><AlertDialogCancel disabled={suppress.isPending}>{t('cancel')}</AlertDialogCancel><Button disabled={suppress.isPending} onClick={suppressForm}>{suppress.isPending ? t('saving') : t('suppressEvidence')}</Button></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
    {suppress.error && <p className="error" role="alert">{formatError(suppress.error)}</p>}

    <h2>{t('history')}</h2>
    <Tabs defaultValue="owner-actions">
      <TabsList><TabsTrigger value="owner-actions">{t('ownerActions')}</TabsTrigger><TabsTrigger value="retained-evidence">{t('retainedEvidence')}</TabsTrigger></TabsList>
      <TabsContent value="owner-actions">
        <p className="muted">{t('historyValuesUnavailable')}</p>
        {history.isError && <p className="error" role="alert">{t('historyUnavailable')} <Button className="secondary" onClick={() => history.refetch()}>{t('retry')}</Button></p>}
        <ul className="stack">{historyRows.map((item) => <li className="card" key={item.id}><strong>{item.operation}</strong><p>{t('recorded')} {formatDateTime(item.recorded_at, display.locale, display.timezone)}</p><p>{t('affectedIds')}: {item.affected_ids.join(', ') || t('none')}</p><p>{t('revisions')}: {Object.entries(item.revisions).map(([id, revision]) => `${id}: ${revision}`).join(', ') || t('none')}</p></li>)}</ul>
        {history.hasNextPage && <Button className="secondary" disabled={history.isFetchingNextPage} onClick={() => history.fetchNextPage()}>{t('loadHistory')}</Button>}
      </TabsContent>
      <TabsContent value="retained-evidence">
        {historyEvidence.isError && <p className="error" role="alert">{t('historyUnavailable')} <Button className="secondary" onClick={() => historyEvidence.refetch()}>{t('retry')}</Button></p>}
        <ul className="stack">{historyEvidenceRows.map((item) => <li className="card" key={item.id}><Link href={`/knowledge/documents/${item.document_id}?version=${item.version_number}#cited-revision`}>{item.title} · {t('documentVersion')} {item.version_number}</Link><small className="muted">{item.metadata_is_version_snapshot ? t('metadataVersionSnapshot') : t('metadataCurrentFallback')}</small><p>{t('observed')} {formatDateTime(item.observed_at, display.locale, display.timezone)}</p><blockquote>{item.excerpt}</blockquote></li>)}</ul>
        {historyEvidence.hasNextPage && <Button className="secondary" disabled={historyEvidence.isFetchingNextPage} onClick={() => historyEvidence.fetchNextPage()}>{t('loadHistory')}</Button>}
      </TabsContent>
    </Tabs>

    <section aria-labelledby="entity-timeline-heading"><h2 id="entity-timeline-heading">{t('entityTimeline')}</h2>
      {entityTimeline.isError && <p className="error" role="alert">{t('timelineUnavailable')} <Button className="secondary" onClick={() => entityTimeline.refetch()}>{t('retry')}</Button></p>}
      {entityTimeline.data && <p className="muted" role="status">{t('graphStatuses')}: {entityGraphStatuses.map((status) => `${status.status}${status.graph_enabled ? '' : ` (${t('graphDisabled')})`}${status.error_code ? ` (${status.error_code})` : ''}`).join(', ') || t('noGraphStatuses')}</p>}
      <div className="stack">{entityEvents.map((event) => <EventDetail key={event.id} event={event} locale={display.locale} timezone={display.timezone} entityNames={new Map([[value.id, value.name ?? t('unnamedEntity')]])} />)}</div>
      {entityTimeline.hasNextPage && <Button className="secondary" disabled={entityTimeline.isFetchingNextPage} onClick={() => entityTimeline.fetchNextPage()}>{t('loadTimeline')}</Button>}
    </section>

    <AlertDialog open={!!aliasRemovalSnapshot} onOpenChange={(open) => { if (!open && !removeAlias.isPending) { setAliasRemovalSnapshot(null); removeAlias.reset(); } }}>
      <AlertDialogContent onEscapeKeyDown={(event) => { if (removeAlias.isPending) event.preventDefault(); }}>
        <AlertDialogHeader><AlertDialogTitle>{t('removeAliasConfirmation')}</AlertDialogTitle><AlertDialogDescription>{t('aliasRemovalDescription')}</AlertDialogDescription></AlertDialogHeader>
        {aliasRemovalSnapshot && <div className="stack"><p>{t('name')}: {aliasRemovalSnapshot.aliasName}</p><p>{t('reason')}: {aliasRemovalSnapshot.reason}</p></div>}
        {removeAlias.error && <p className="error" role="alert">{t('aliasFailed')} {formatError(removeAlias.error)}</p>}
        <AlertDialogFooter><AlertDialogCancel disabled={removeAlias.isPending}>{t('cancel')}</AlertDialogCancel><Button disabled={!aliasRemovalSnapshot || removeAlias.isPending} onClick={confirmAliasRemoval}>{removeAlias.isPending ? t('saving') : t('removeAlias')}</Button></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>

    <h2>{t('relationships')}</h2>
    {neighbors.isError && <p className="error" role="alert">{t('relationshipUnavailable')} <Button className="secondary" onClick={() => neighbors.refetch()}>{t('retry')}</Button></p>}
    <ul className="stack">{neighborsItems.map(({ entity: neighbor, relationship }) => <li className="card" key={relationship.id}>
      <Link href={`/knowledge/entities/${neighbor.id}`}>{neighbor.name ?? t(`type_${neighbor.type}` as 'type_person')}</Link><span> — {relationship.type} ({relationship.origin})</span>
      <Button type="button" className="secondary" aria-pressed={selectedRelationshipId === relationship.id} onClick={() => setSelectedRelationshipId(relationship.id)}>{t('selectRelationship')}</Button>
    </li>)}</ul>
    {neighbors.hasNextPage && <Button className="secondary" disabled={neighbors.isFetchingNextPage} onClick={() => neighbors.fetchNextPage()}>{t('loadRelationships')}</Button>}
    {!neighborsItems.length && <p className="muted">{t('noRelationships')}</p>}
    <EntityGraph entityId={value.id} title={value.name ?? t('unnamedEntity')} selectedRelationshipId={selectedRelationshipId ?? undefined} onSelectRelationship={setSelectedRelationshipId} />
    <section aria-labelledby="relationship-evidence-heading"><h3 id="relationship-evidence-heading">{t('relationshipEvidence')}</h3>
      {!selectedRelationshipId && <p className="muted">{t('relationshipSelected')}</p>}
      {relationshipEvidence.isError && <p className="error" role="alert">{t('relationshipUnavailable')} <Button className="secondary" onClick={() => relationshipEvidence.refetch()}>{t('retry')}</Button></p>}
      {relationshipRows.map((item) => <article className="card" key={item.id}><p><Link href={`/knowledge/documents/${item.document_id}?version=${item.version_number}#cited-revision`}>{item.title} · {t('documentVersion')} {item.version_number}</Link> · {t('source')} {item.source_id}</p><small className="muted">{item.metadata_is_version_snapshot ? t('metadataVersionSnapshot') : t('metadataCurrentFallback')}</small><blockquote>{item.excerpt}</blockquote><small>{t('observed')} {formatDateTime(item.observed_at, display.locale, display.timezone)} · {t('confidence')} {Number.isFinite(item.confidence) ? item.confidence.toFixed(2) : t('unknownValue')}</small><p>{t('citationCount')}: 1 · {t('membership')} {item.source_entity_membership_id ?? t('unknownValue')} / {item.target_entity_membership_id ?? t('unknownValue')}</p></article>)}
      {relationshipEvidence.hasNextPage && <Button className="secondary" disabled={relationshipEvidence.isFetchingNextPage} onClick={() => relationshipEvidence.fetchNextPage()}>{t('loadEvidence')}</Button>}
    </section>

    <h2>{t('split')}</h2><p className="muted">{t('splitHelp')}</p>
    <div className="field"><Label htmlFor="split-name">{t('newEntityName')}</Label><Input id="split-name" maxLength={300} aria-invalid={!!correction.formState.errors.splitName} {...correction.register('splitName')} onChange={(event) => { correction.setValue('splitName', event.target.value, { shouldDirty: true }); correction.clearErrors('splitName'); }} />{correction.formState.errors.splitName && <p className="error" role="alert">{t('splitNameRequired')}</p>}</div>
    <Button className="secondary" disabled={!selected.length || staleSelection || splitPreview.isPending} onClick={previewSplitForm}>{t('previewSplit')}</Button>
    {splitPreview.error && <p className="error" role="alert">{t('splitPreviewFailed')} {formatError(splitPreview.error)}</p>}
    {currentSplitPreview && <div className="card" role="status"><p>{t('correctionScope')}: {currentSplitPreview.preview.entity_ids.length} · {t('memberships')}: {currentSplitPreview.preview.membership_ids.length} · {t('relationshipCount')}: {currentSplitPreview.preview.relationship_ids.length} · {t('evidenceReferences')}: {currentSplitPreview.preview.evidence_ref_count}</p><p>{t('selectedMemberships')}: {currentSplitPreview.payload.evidence_ids.slice(0, 20).join(', ')}{currentSplitPreview.payload.evidence_ids.length > 20 ? ` · ${t('additionalItems', { count: currentSplitPreview.payload.evidence_ids.length - 20 })}` : ''}</p><p>{t('reason')}: {currentSplitPreview.payload.reason} · {t('revision')} {currentSplitPreview.payload.expected_revision}</p>{currentSplitPreview.preview.conflicts.map((conflict) => <p className="error" key={conflict.code}>{conflict.code}: {conflict.message} · {conflict.entity_ids.slice(0, 5).join(', ')} · {conflict.membership_ids.slice(0, 5).join(', ')} · {conflict.relationship_ids.slice(0, 5).join(', ')}</p>)}<AlertDialog><AlertDialogTrigger asChild><Button type="button" disabled={split.isPending || !!currentSplitPreview.preview.conflicts.length}>{t('confirmSplit')}</Button></AlertDialogTrigger><AlertDialogContent onEscapeKeyDown={(event) => { if (split.isPending) event.preventDefault(); }}><AlertDialogHeader><AlertDialogTitle>{t('confirmSplit')}</AlertDialogTitle><AlertDialogDescription>{t('splitConfirmationDescription')}</AlertDialogDescription></AlertDialogHeader><div className="stack"><p>{t('newEntityName')}: {currentSplitPreview.payload.new_entity.name} · {t(`type_${currentSplitPreview.payload.new_entity.type}` as 'type_person')}</p><p>{t('revision')}: {currentSplitPreview.payload.expected_revision} · {t('memberships')}: {currentSplitPreview.payload.evidence_ids.length} · {t('relationshipCount')}: {currentSplitPreview.preview.relationship_ids.length} · {t('evidenceReferences')}: {currentSplitPreview.preview.evidence_ref_count}</p><p>{t('reason')}: {currentSplitPreview.payload.reason}</p><p>{t('selectedMemberships')}: {currentSplitPreview.payload.evidence_ids.slice(0, 20).join(', ')}{currentSplitPreview.payload.evidence_ids.length > 20 ? ` · ${t('additionalItems', { count: currentSplitPreview.payload.evidence_ids.length - 20 })}` : ''}</p></div>{split.error && <div className="error" role="alert"><p>{t('correctionWriteFailed')} {formatError(split.error)}</p>{conflictDetails(split.error).map((line, index) => <p key={index}>{line}</p>)}</div>}<AlertDialogFooter><AlertDialogCancel disabled={split.isPending}>{t('cancel')}</AlertDialogCancel><Button disabled={split.isPending} onClick={confirmSplitForm}>{split.isPending ? t('saving') : t('confirmSplit')}</Button></AlertDialogFooter></AlertDialogContent></AlertDialog></div>}
    {splitPreview.data && !currentSplitPreview && <p className="error" role="status">{t('previewStale')}</p>}

    <h2>{t('merge')}</h2><p className="muted">{t('mergeHelp')}</p>
    <div className="field"><Label htmlFor="merge-target-search">{t('findMergeTarget')}</Label><Input id="merge-target-search" type="search" value={targetSearch} onChange={(event) => { setTargetSearch(event.target.value); setSelectedMergeTarget(null); correction.setValue('targetId', '', { shouldDirty: true }); correction.clearErrors('targetId'); mergePreview.reset(); }} /></div>
    {targetSearch.trim().length >= 2 && <div className="field"><Label htmlFor="merge-target">{t('mergeTarget')}</Label><Select value={selectedMergeTarget?.id ?? ''} onValueChange={(id) => { const chosen = targetItems.find((item) => item.id === id); if (!chosen) return; setSelectedMergeTarget(chosen); correction.setValue('targetId', chosen.id, { shouldDirty: true }); correction.clearErrors('targetId'); mergePreview.reset(); }}><SelectTrigger id="merge-target"><SelectValue placeholder={t('chooseMergeTarget')} /></SelectTrigger><SelectContent>{targetOptions.map((item) => <SelectItem key={item.id} value={item.id}>{item.name ?? t('unnamedEntity')} · {t(`type_${item.type}` as 'type_person')} · {t('revision')} {item.revision}</SelectItem>)}</SelectContent></Select>{selectedMergeTarget && <p className="muted">{t('selectedMergeTarget')}: {selectedMergeTarget.name ?? t('unnamedEntity')} · {t('revision')} {selectedMergeTarget.revision}</p>}{correction.formState.errors.targetId && <p className="error" role="alert">{t('mergeTargetRequired')}</p>}{targets.isError && <p className="error" role="alert">{targets.error.message} <Button type="button" className="secondary" onClick={() => targets.refetch()}>{t('retry')}</Button></p>}{targets.hasNextPage && <Button type="button" className="secondary" disabled={targets.isFetchingNextPage} onClick={() => targets.fetchNextPage()}>{t('loadMore')}</Button>}</div>}
    <div className="field"><Label htmlFor="correction-reason">{t('reason')}</Label><Input id="correction-reason" maxLength={300} aria-invalid={!!correction.formState.errors.reason} {...correction.register('reason')} />{correction.formState.errors.reason && <p className="error" role="alert">{t('correctionReasonRequired')}</p>}</div>
    <Button className="secondary" disabled={!selectedMergeTarget || mergePreview.isPending} onClick={previewMergeForm}>{t('previewMerge')}</Button>
    {mergePreview.error && <p className="error" role="alert">{t('mergePreviewFailed')} {formatError(mergePreview.error)}</p>}
    {currentMergePreview && <div className="card" role="status"><p>{t('mergeInto')} {currentMergePreview.targetName} · {t('revision')} {currentMergePreview.intoRevision}</p><p>{t('sourceRevision')}: {currentMergePreview.sourceRevision} · {t('reason')}: {currentMergePreview.reason}</p><p>{t('entitiesCount')}: {currentMergePreview.preview.entity_ids.length} · {t('memberships')}: {currentMergePreview.preview.membership_ids.length} · {t('relationshipCount')}: {currentMergePreview.preview.relationship_ids.length} · {t('evidenceReferences')}: {currentMergePreview.preview.evidence_ref_count}</p><p>{t('selectedMemberships')}: {currentMergePreview.preview.membership_ids.slice(0, 20).join(', ')}{currentMergePreview.preview.membership_ids.length > 20 ? ` · ${t('additionalItems', { count: currentMergePreview.preview.membership_ids.length - 20 })}` : ''}</p>{currentMergePreview.preview.conflicts.map((conflict) => <p className="error" key={conflict.code}>{conflict.code}: {conflict.message} · {conflict.entity_ids.slice(0, 5).join(', ')} · {conflict.membership_ids.slice(0, 5).join(', ')} · {conflict.relationship_ids.slice(0, 5).join(', ')}</p>)}<AlertDialog><AlertDialogTrigger asChild><Button type="button" disabled={merge.isPending || !!currentMergePreview.preview.conflicts.length}>{t('confirmMerge')}</Button></AlertDialogTrigger><AlertDialogContent onEscapeKeyDown={(event) => { if (merge.isPending) event.preventDefault(); }}><AlertDialogHeader><AlertDialogTitle>{t('confirmMerge')}</AlertDialogTitle><AlertDialogDescription>{t('mergeConfirmationDescription')}</AlertDialogDescription></AlertDialogHeader><div className="stack"><p>{t('mergeInto')} {currentMergePreview.targetName} · {t('revision')} {currentMergePreview.intoRevision}</p><p>{t('sourceRevision')}: {currentMergePreview.sourceRevision} · {t('reason')}: {currentMergePreview.reason}</p><p>{t('entitiesCount')}: {currentMergePreview.preview.entity_ids.length} · {t('memberships')}: {currentMergePreview.preview.membership_ids.length} · {t('relationshipCount')}: {currentMergePreview.preview.relationship_ids.length} · {t('evidenceReferences')}: {currentMergePreview.preview.evidence_ref_count}</p><p>{t('selectedMemberships')}: {currentMergePreview.preview.membership_ids.slice(0, 20).join(', ')}{currentMergePreview.preview.membership_ids.length > 20 ? ` · ${t('additionalItems', { count: currentMergePreview.preview.membership_ids.length - 20 })}` : ''}</p></div>{merge.error && <div className="error" role="alert"><p>{t('correctionWriteFailed')} {formatError(merge.error)}</p>{conflictDetails(merge.error).map((line, index) => <p key={index}>{line}</p>)}</div>}<AlertDialogFooter><AlertDialogCancel disabled={merge.isPending}>{t('cancel')}</AlertDialogCancel><Button disabled={merge.isPending} onClick={confirmMergeForm}>{merge.isPending ? t('saving') : t('confirmMerge')}</Button></AlertDialogFooter></AlertDialogContent></AlertDialog></div>}
    {mergePreview.data && !currentMergePreview && <p className="error" role="status">{t('previewStale')}</p>}
    {correctionErrors.map((error, index) => <div key={index} className="error" role="alert">{formatError(error!)}{conflictDetails(error).map((line, i) => <p key={i}>{line}</p>)}</div>)}
  </section>;
}
