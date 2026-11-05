import { usePagination } from '@/components/ui/pagination';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams, useRouter } from 'next/navigation';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { useTranslations } from 'next-intl';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import { useDisplayPreferences } from '@/core/query-provider';
import { useWorkspace } from '@/core/workspace-context';
import { addEntityAlias, deleteEntityAlias, entityKeys, getEntity, getEntityNeighbors, getEntityTimeline, getRelationshipEvidence, listEntities, listEntityEvidence, listEntityHistory, mergeEntity, previewMergeEntity, previewSplitEntity, splitEntity, suppressEntityEvidence, updateEntity, type Entity } from './api';
import { aliasSchema, correctionSchema, identitySchema, type CorrectionValues, type IdentityValues } from './entity-detail-schemas';

/** Owns the entity detail screen's local draft state: forms, selections, dialogs and the unsaved-change flag. */
export function useEntityDraftState() {
  const t = useTranslations('entities');
  const { entityId } = useParams<{ entityId: string }>();
  const router = useRouter();
  const session = useWorkspaceSession();
  const { isAdminViewer } = useWorkspace();
  const display = useDisplayPreferences();
  const client = useQueryClient();
  const { registerLeaveGuard, ensureEntityDocument } = useGuardedNavigation();
  const [documentReadyId, setDocumentReadyId] = useState<string | null>(null);
  const [tab, setTab] = useState('overview');
  const [copied, setCopied] = useState(false);
  const chat = useChatController();
  const [selected, setSelected] = useState<string[]>([]);
  const [selectionRevision, setSelectionRevision] = useState<number | null>(null);
  const [selectedRelationshipId, setSelectedRelationshipId] = useState<string | null>(null);
  const [targetSearch, setTargetSearch] = useState('');
  const [selectedMergeTarget, setSelectedMergeTarget] = useState<Entity | null>(null);
  const [suppressConfirmed, setSuppressConfirmed] = useState(false);
  const [suppressSnapshot, setSuppressSnapshot] = useState<{ evidenceIds: string[]; revision: number; reason: string } | null>(null);
  const [aliasRemovalSnapshot, setAliasRemovalSnapshot] = useState<{ aliasId: string; aliasName: string; reason: string } | null>(null);
  const baselineRef = useRef<{ id: string; revision: number; name: string; description: string } | null>(null);
  const identity = useForm<IdentityValues>({ resolver: zodResolver(identitySchema), defaultValues: { name: '', description: '', reason: 'owner_review' } });
  const aliasForm = useForm<z.infer<typeof aliasSchema>>({ resolver: zodResolver(aliasSchema), defaultValues: { alias: '', reason: 'owner_review' } });
  const correction = useForm<CorrectionValues>({ resolver: zodResolver(correctionSchema), mode: 'onChange', defaultValues: { targetId: '', splitName: '', reason: 'owner_review' } });
  const identityDirty = identity.formState.isDirty;
  const aliasDirty = aliasForm.formState.isDirty;
  const correctionDirty = correction.formState.isDirty;
  const dirtyRef = useRef(false);

  /** Mirrors the unsaved-change flag for the leave guard, which reads it at navigation time. */
  useLayoutEffect(() => { dirtyRef.current = identityDirty || aliasDirty || correctionDirty || selected.length > 0 || suppressConfirmed; }, [identityDirty, aliasDirty, correctionDirty, selected.length, suppressConfirmed]);

  return { t, entityId, router, session, isAdminViewer, display, client, registerLeaveGuard, ensureEntityDocument, documentReadyId, setDocumentReadyId, tab, setTab, copied, setCopied, chat, selected, setSelected, selectionRevision, setSelectionRevision, selectedRelationshipId, setSelectedRelationshipId, targetSearch, setTargetSearch, selectedMergeTarget, setSelectedMergeTarget, suppressConfirmed, setSuppressConfirmed, suppressSnapshot, setSuppressSnapshot, aliasRemovalSnapshot, setAliasRemovalSnapshot, baselineRef, identity, aliasForm, correction, dirtyRef };
}

type DraftState = ReturnType<typeof useEntityDraftState>;

/** Loads the entity and its paged evidence, history, timeline, neighbour, relationship-evidence and merge-target queries. */
export function useEntityQueries(p: DraftState) {
  const { entityId, display, client, ensureEntityDocument, setDocumentReadyId, selectedRelationshipId, targetSearch, selectedMergeTarget, correction } = p;
  const entity = useQuery({ queryKey: entityKeys.detail(entityId), queryFn: () => getEntity(entityId) });
  useEffect(() => { if (ensureEntityDocument()) setDocumentReadyId(entityId); }, [ensureEntityDocument, entityId, setDocumentReadyId]);
  const evidence = useInfiniteQuery({ queryKey: entityKeys.evidence(entityId), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntityEvidence(entityId, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const history = useInfiniteQuery({ queryKey: entityKeys.history(entityId), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntityHistory(entityId, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const historyEvidence = useInfiniteQuery({ queryKey: [...entityKeys.history(entityId), 'memberships'], initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntityHistory(entityId, undefined, pageParam), getNextPageParam: (page) => page.membership_next_cursor ?? undefined });
  const entityTimeline = useInfiniteQuery({ queryKey: entityKeys.timeline(entityId, { timezone: display.timezone }), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => getEntityTimeline(entityId, { timezone: display.timezone }, pageParam), getNextPageParam: (page) => page.timeline.next_cursor ?? undefined });
  const neighbors = useInfiniteQuery({ queryKey: entityKeys.neighbors(entityId), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => getEntityNeighbors(entityId, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const relationshipEvidence = useInfiniteQuery({ queryKey: ['relationships', selectedRelationshipId, 'evidence'], enabled: !!selectedRelationshipId, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => getRelationshipEvidence(selectedRelationshipId!, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  correction.watch('targetId'); // subscribes the form so target changes re-render the merge controls
  const splitName = correction.watch('splitName');
  const reason = correction.watch('reason');
  const targets = useInfiniteQuery({ queryKey: entityKeys.list(entity.data?.type, targetSearch), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntities(pageParam, entity.data?.type, targetSearch.trim()), getNextPageParam: (page) => page.next_cursor ?? undefined, enabled: !!entity.data && targetSearch.trim().length >= 2 });
  const targetItems = targets.data?.pages.flatMap((page) => page.items).filter((item) => item.id !== entity.data?.id) ?? [];
  const targetOptions = targetItems.map((item) => selectedMergeTarget?.id === item.id ? selectedMergeTarget : item);
  /** Invalidates entity and relationship queries after a successful correction. */
  const saved = () => { void client.invalidateQueries({ queryKey: entityKeys.all }); void client.invalidateQueries({ queryKey: ['relationships'] }); };
  return { entity, evidence, history, historyEvidence, entityTimeline, neighbors, relationshipEvidence, splitName, reason, targets, targetItems, targetOptions, saved };
}

type DraftQueries = DraftState & ReturnType<typeof useEntityQueries>;

/** Registers the unsaved-change guard and keeps the identity form baseline in sync with the loaded entity. */
function useEntityLeaveGuard(p: DraftQueries) {
  const { t, registerLeaveGuard, selected, setSelected, selectionRevision, setSelectionRevision, setTargetSearch, setSelectedMergeTarget, setSuppressConfirmed, setSuppressSnapshot, baselineRef, identity, dirtyRef, entity, reason } = p;
  const resetIdentity = p.identity.reset;
  const resetAlias = p.aliasForm.reset;
  const resetCorrection = p.correction.reset;
  useEffect(() => {
    const value = entity.data;
    if (!value || identity.formState.isDirty) return;
    baselineRef.current = { id: value.id, revision: value.revision, name: value.name ?? '', description: value.description ?? '' };
    resetIdentity({ name: value.name ?? '', description: value.description ?? '', reason: 'owner_review' });
  }, [entity.data, identity.formState.isDirty, resetIdentity, baselineRef]);
  /** Asks before discarding the current unsaved draft. */
  const confirmDiscard = useCallback(() => window.confirm(t('discardChangesQuestion')), [t]);
  /** Invalidates the current draft session and accepts leaving the editor. */
  const acceptLeave = useCallback(() => {
    dirtyRef.current = false;
    baselineRef.current = null;
    resetIdentity({ name: '', description: '', reason: 'owner_review' });
    resetAlias({ alias: '', reason: 'owner_review' });
    resetCorrection({ targetId: '', splitName: '', reason: 'owner_review' });
    setSelected([]);
    setTargetSearch('');
    setSelectedMergeTarget(null);
    setSelectionRevision(null);
    setSuppressConfirmed(false);
  }, [resetIdentity, resetAlias, resetCorrection, baselineRef, dirtyRef, setSelected, setTargetSearch, setSelectedMergeTarget, setSelectionRevision, setSuppressConfirmed]);
  useEffect(() => registerLeaveGuard({
    hasUnsavedChanges: () => dirtyRef.current,
    confirmDiscard,
    acceptLeave,
  }), [registerLeaveGuard, confirmDiscard, acceptLeave, dirtyRef]);
  useEffect(() => { setSuppressConfirmed(false); setSuppressSnapshot(null); }, [selected, selectionRevision, entity.data?.revision, reason, setSuppressConfirmed, setSuppressSnapshot]);
}

/** Declares the identity, alias, merge, suppression and split mutations; each sends the session CSRF token and invalidates entity queries on success. */
function useEntityMutations(p: DraftQueries) {
  const { t, entityId, router, session, client, selected, setSelected, selectionRevision, setSelectionRevision, setTargetSearch, selectedMergeTarget, setSelectedMergeTarget, setSuppressSnapshot, setAliasRemovalSnapshot, baselineRef, identity, aliasForm, correction, entity, evidence, saved } = p;
  const resetCorrection = p.correction.reset;
  const update = useMutation({
    mutationFn: (values: IdentityValues) => {
      const original = baselineRef.current;
      if (!original) throw new Error(t('reloadReview'));
      const payload: { expected_revision: number; name?: string; description?: string | null; reason: string } = { expected_revision: original.revision, reason: values.reason };
      if (identity.formState.dirtyFields.name) payload.name = values.name.trim();
      if (identity.formState.dirtyFields.description) payload.description = values.description.trim() || null;
      if (!identity.formState.dirtyFields.name && !identity.formState.dirtyFields.description) throw new Error(t('saveIdentity'));
      return updateEntity(original.id, payload, session.csrfToken);
    },
    onSuccess: (value) => { baselineRef.current = { id: value.id, revision: value.revision, name: value.name ?? '', description: value.description ?? '' }; identity.reset({ name: value.name ?? '', description: value.description ?? '', reason: 'owner_review' }); client.setQueryData(entityKeys.detail(entityId), value); saved(); },
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
  const resetMergePreview = mergePreview.reset;
  useEffect(() => {
    setTargetSearch('');
    setSelectedMergeTarget(null);
    resetCorrection({ targetId: '', splitName: '', reason: 'owner_review' });
    resetMergePreview();
  }, [entityId, resetCorrection, resetMergePreview, setTargetSearch, setSelectedMergeTarget]);
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
  return { update, addAlias, removeAlias, mergePreview, merge, suppress, splitPayload, splitPreview, split };
}

/** Composes the entity detail state, queries, leave guard and mutations into the single controller consumed by the screen. */
export function useEntityDetailController() {
  const draft = useEntityDraftState();
  const queries = useEntityQueries(draft);
  const base = { ...draft, ...queries };
  useEntityLeaveGuard(base);
  const mutations = useEntityMutations(base);
  const neighborsPager = usePagination(queries.neighbors.data?.pages.flatMap((page) => page.items) ?? []);
  return { ...base, ...mutations, neighborsPager };
}
