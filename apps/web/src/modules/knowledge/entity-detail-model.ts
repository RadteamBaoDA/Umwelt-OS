import { ApiError } from '@/core/api';
import { type Entity } from './api';
import { mergeConfirmationSchema, splitConfirmationSchema } from './entity-detail-schemas';
import type { useEntityDetailController } from './use-entity-detail';

type EntityDetailController = ReturnType<typeof useEntityDetailController>;
type MergePreviewData = EntityDetailController['mergePreview']['data'];
type SplitPreviewData = EntityDetailController['splitPreview']['data'];

/** Flattens an infinite query's loaded pages into one list using the supplied per-page selector. */
export function pagesOf<P, T>(data: { pages: P[] } | undefined, pick: (page: P) => T[]): T[] {
  return data?.pages.flatMap(pick) ?? [];
}

/** Returns the merge preview only while it still matches the entity revision, chosen target and trimmed reason. */
export function matchingMergePreview(preview: MergePreviewData | undefined, value: Entity, target: Entity | null, reason: string) {
  if (!preview) return null;
  const current = preview.sourceRevision === value.revision && preview.intoId === target?.id
    && preview.reason === reason.trim() && preview.intoRevision === target?.revision;
  return current ? preview : null;
}

/** Returns the split preview only while its revision, name, reason and evidence selection are unchanged. */
export function matchingSplitPreview(preview: SplitPreviewData | undefined, selectionRevision: number | null, selected: string[], splitName: string, reason: string) {
  if (!preview) return null;
  const { payload } = preview;
  const sameSelection = payload.evidence_ids.length === selected.length && payload.evidence_ids.every((id) => selected.includes(id));
  const current = payload.expected_revision === selectionRevision && payload.new_entity.name === splitName.trim()
    && payload.reason === reason.trim() && sameSelection;
  return current ? preview : null;
}

/** Display state derived from a loaded entity; pure (no hooks) so it can be computed after the loading guards. */
function deriveEntityState(c: EntityDetailController, value: Entity) {
  const { selected, selectionRevision, selectedMergeTarget, baselineRef, identity, correction, evidence, history, historyEvidence, entityTimeline, neighbors, relationshipEvidence, splitName, reason, update, mergePreview, merge, suppress, splitPreview, split } = c;
  const evidenceItems = pagesOf(evidence.data, (page) => page.items);
  const neighborsItems = pagesOf(neighbors.data, (page) => page.items);
  const currentMergePreview = matchingMergePreview(mergePreview.data, value, selectedMergeTarget, reason);
  const currentSplitPreview = matchingSplitPreview(splitPreview.data, selectionRevision, selected, splitName, reason);
  const correctionErrors = [merge.error, mergePreview.error, split.error, splitPreview.error, suppress.error].filter(Boolean);
  /** Maps a document or entity API error to the feature’s displayable error state. */
  const formatError = (error: Error) => (error instanceof ApiError ? `${error.message}${error.code ? ` (${error.code})` : ''}` : error.message).slice(0, 500);
  const baselineSnapshot = baselineRef.current;
  const staleIdentity = !!baselineSnapshot && (baselineSnapshot.revision !== value.revision || baselineSnapshot.id !== value.id);
  const staleSelection = selected.length > 0 && selectionRevision !== value.revision;
  const relationshipRows = pagesOf(relationshipEvidence.data, (page) => page.items);
  const historyRows = pagesOf(history.data, (page) => page.items);
  const historyEvidenceRows = pagesOf(historyEvidence.data, (page) => page.memberships);
  const entityEvents = pagesOf(entityTimeline.data, (page) => page.timeline.items);
  const entityGraphStatuses = pagesOf(entityTimeline.data, (page) => page.graph_statuses);
  /** Clears pending identity edits without committing them. */
  const clearIdentityDraft = () => { baselineRef.current = { id: value.id, revision: value.revision, name: value.name ?? '', description: value.description ?? '' }; identity.reset({ name: value.name ?? '', description: value.description ?? '', reason: 'owner_review' }); update.reset(); };
  /** Keeps the current draft but rebases it on the latest loaded revision. */
  const keepIdentityDraft = () => { baselineRef.current = { id: value.id, revision: value.revision, name: value.name ?? '', description: value.description ?? '' }; };
  /** Stores the latest correction preview or submission issue for display. */
  const setCorrectionIssue = (field: 'targetId' | 'splitName' | 'reason', message: string) => correction.setError(field, { type: 'manual', message });
  return { evidenceItems, neighborsItems, currentMergePreview, currentSplitPreview, correctionErrors, formatError, baselineSnapshot, keepIdentityDraft, staleIdentity, staleSelection, relationshipRows, historyRows, historyEvidenceRows, entityEvents, entityGraphStatuses, clearIdentityDraft, setCorrectionIssue };
}

type DerivedEntityState = ReturnType<typeof deriveEntityState>;

/** Builds the form submit handlers and UI actions for a loaded entity; handlers read the latest snapshots at call time. */
function buildEntityActions(c: EntityDetailController, d: DerivedEntityState, value: Entity) {
  const { t, setTab, setCopied, chat, selected, setSelected, selectionRevision, setSelectionRevision, selectedMergeTarget, suppressConfirmed, suppressSnapshot, aliasRemovalSnapshot, setAliasRemovalSnapshot, correction, removeAlias, mergePreview, merge, suppress, splitPreview, split } = c;
  const { currentMergePreview, currentSplitPreview, staleSelection, setCorrectionIssue } = d;
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

  const unconfirmedAliases = value.aliases.filter((item) => !item.confirmed);
  /** Copies a plain-text citation (name, type and page address) for this entity to the clipboard. */
  const copyCitation = () => {
    const citation = `${value.name ?? t('unnamedEntity')} (${t(`type_${value.type}` as 'type_person')}, ${t('revision')} ${value.revision}) ${window.location.href}`;
    void navigator.clipboard?.writeText(citation).then(() => { setCopied(true); window.setTimeout(() => setCopied(false), 2000); }).catch(() => undefined);
  };
  /** Opens the Chat drawer scoped to this entity. */
  const askAi = () => chat.openDrawer({ context: { kind: 'entity', resource_id: value.id } });
  /** Returns to Overview and moves focus to the identity name field. */
  const correctIdentity = () => { setTab('overview'); window.setTimeout(() => document.getElementById('entity-name')?.focus(), 0); };
  /** Shared underline-style class for the tab strip triggers. */
  const tabClass = 'min-h-10 rounded-none border-b-2 border-transparent bg-transparent px-3 data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none';
  return { previewMergeForm, confirmMergeForm, previewSplitForm, confirmSplitForm, openAliasRemoval, confirmAliasRemoval, suppressForm, addSelectedEvidence, unconfirmedAliases, copyCitation, askAi, correctIdentity, tabClass };
}

/** Combines controller state, derived display state and actions into the view model rendered once the entity is loaded. */
export function buildEntityViewModel(c: EntityDetailController, value: Entity) {
  const derived = deriveEntityState(c, value);
  const actions = buildEntityActions(c, derived, value);
  return { ...c, ...derived, ...actions, value };
}

export type EntityDetailViewModel = ReturnType<typeof buildEntityViewModel>;
