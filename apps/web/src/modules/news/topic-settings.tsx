'use client';

import { useState } from 'react';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { ApiError } from '@/core/api';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { createTopic, deleteTopic, fetchTopics, getTopic, searchTopicEntities, updateTopic } from './api';
import type { Topic, TopicCreate, TopicUpdate } from './types';

type TopicDraft = { name: string; description: string; keywords: string; entityIds: string[]; isActive: boolean; weight: string };

/**
 * Return an editable copy of the topic, keeping form values independent of query cache updates.
 * @param topic Existing profile or null for a new interest.
 */
function makeDraft(topic: Topic | null): TopicDraft {
  return { name: topic?.name ?? '', description: topic?.description ?? '', keywords: topic?.keywords.join(', ') ?? '', entityIds: topic?.entity_ids ?? [], isActive: topic?.is_active ?? true, weight: String(topic?.weight ?? 1) };
}

/** Normalize comma-separated keyword input for API submission and client bound checks. */
function parseKeywords(value: string): string[] {
  return [...new Set(value.split(',').map((item) => item.trim().replace(/\s+/g, ' ')).filter(Boolean))];
}

/**
 * Edit owner topics through bounded cursor pages and public APIs, retaining drafts during server revision review.
 * Writes require trusted CSRF and the expected server revision; pending page/mutation/reload operations are guarded.
 * Conflict recovery compares current fields beside the draft and requires explicit continuation before save.
 * Deletion is confirmed and tombstones the profile, removing it from current tracked interests.
 * @param csrfToken Trusted authenticated-session token required for every mutation.
 */
export function TopicSettings({ csrfToken }: { csrfToken: string }) {
  const t = useTranslations('topics');
  const [editing, setEditing] = useState<Topic | null | undefined>(undefined);
  const [draft, setDraft] = useState<TopicDraft>(makeDraft(null));
  const [deleteTarget, setDeleteTarget] = useState<Topic | null>(null);
  const [entityQuery, setEntityQuery] = useState('');
  const [conflictTopic, setConflictTopic] = useState<Topic | null>(null);
  const [conflictCurrent, setConflictCurrent] = useState<Topic | null>(null);
  const [conflictReviewed, setConflictReviewed] = useState(false);
  const [conflictReloadPending, setConflictReloadPending] = useState(false);
  const [conflictReloadFailed, setConflictReloadFailed] = useState(false);
  const queryClient = useQueryClient();
  const topicsQuery = useInfiniteQuery({
    queryKey: ['topics'],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => fetchTopics({ cursor: pageParam }),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const entitiesQuery = useQuery({ queryKey: ['topic-entity-options', entityQuery], queryFn: () => searchTopicEntities(entityQuery) });

  const saveMutation = useMutation({
    mutationFn: async () => {
      const common = {
        name: draft.name.trim(), description: draft.description.trim() || null,
        keywords: parseKeywords(draft.keywords),
        entity_ids: draft.entityIds, is_active: draft.isActive, weight: Number(draft.weight),
      };
      if (editing) return updateTopic(editing.id, { ...common, expected_revision: editing.revision } satisfies TopicUpdate, csrfToken);
      return createTopic(common satisfies TopicCreate, csrfToken);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['topics'] });
      setEditing(undefined);
      setConflictTopic(null);
      setConflictCurrent(null);
      setConflictReviewed(false);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409 && editing) {
        setConflictTopic(editing);
        setConflictCurrent(null);
        setConflictReviewed(false);
        setConflictReloadFailed(false);
      }
    },
  });
  const activeMutation = useMutation({
    mutationFn: (topic: Topic) => updateTopic(topic.id, { expected_revision: topic.revision, is_active: !topic.is_active }, csrfToken),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['topics'] }); },
  });
  const deleteMutation = useMutation({
    mutationFn: (topic: Topic) => deleteTopic(topic.id, topic.revision, csrfToken),
    onSuccess: () => { setDeleteTarget(null); void queryClient.invalidateQueries({ queryKey: ['topics'] }); },
  });

  /**
   * Opens a fresh isolated draft so cancelling never writes query data.
   * @param topic Selected profile or null to create a new one.
   */
  function beginEdit(topic: Topic | null) {
    setEditing(topic);
    setDraft(makeDraft(topic));
    saveMutation.reset();
    setConflictTopic(null);
    setConflictCurrent(null);
    setConflictReviewed(false);
    setConflictReloadFailed(false);
  }

  /**
   * Fetches current server state while preserving the stale revision and unsaved draft.
   * @returns Whether the server version loaded for side-by-side review.
   */
  async function reloadConflict() {
    if (!editing || !conflictTopic || conflictReloadPending) return false;
    setConflictReloadPending(true);
    setConflictReloadFailed(false);
    try {
      setConflictCurrent(await getTopic(editing.id));
      setConflictReviewed(false);
      return true;
    } catch {
      setConflictReloadFailed(true);
      return false;
    } finally {
      setConflictReloadPending(false);
    }
  }

  /**
   * Accepts the explicitly reviewed server revision as the fence for the retained draft.
   * @remarks The current server values remain visible so the user can compare them while saving.
   */
  function continueAfterConflictReview() {
    if (!conflictCurrent || conflictReloadPending || saveMutation.isPending) return;
    setEditing(conflictCurrent);
    setConflictReviewed(true);
    setConflictReloadFailed(false);
  }

  /**
   * Toggle a canonical public entity ID in the current unsaved draft.
   * @param id Canonical entity identifier from the public knowledge API.
   * @param checked Whether the current draft should include the reference.
   */
  function toggleEntity(id: string, checked: boolean) {
    setDraft((value) => ({ ...value, entityIds: checked ? [...value.entityIds, id].slice(0, 100) : value.entityIds.filter((item) => item !== id) }));
  }

  const topics = topicsQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const totalTopics = topicsQuery.data?.pages[0]?.total ?? 0;
  const isPending = saveMutation.isPending || activeMutation.isPending || deleteMutation.isPending || topicsQuery.isFetchingNextPage || conflictReloadPending;
  const keywordValues = parseKeywords(draft.keywords);
  const keywordsInBounds = keywordValues.length <= 50 && keywordValues.every((item) => item.length <= 100);

  return (
    <section className="flex flex-col gap-5" aria-labelledby="topic-settings-title">
      <header>
        <h2 id="topic-settings-title" className="text-lg font-semibold">{t('title')}</h2>
        <p className="text-sm text-muted-foreground">{t('description')}</p>
      </header>
      <div className="flex justify-end"><Button type="button" onClick={() => beginEdit(null)} disabled={isPending}>{t('add')}</Button></div>
      {topicsQuery.isLoading ? <p role="status" className="text-sm text-muted-foreground">{t('loading')}</p> : null}
      {topicsQuery.error && !topicsQuery.data ? <p role="alert" className="text-sm text-destructive">{t('loadFailed')}</p> : null}
      {topics.length > 0 && <p className="text-xs text-muted-foreground">{t('totalCount', { loaded: topics.length, total: totalTopics })}</p>}
      {!topicsQuery.isLoading && !topicsQuery.error && topics.length === 0 ? <p className="text-sm text-muted-foreground">{t('empty')}</p> : null}
      <ul className="flex flex-col gap-2">
        {topics.map((topic) => (
          <li key={topic.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3">
            <div className="flex min-w-0 items-start gap-3">
              <Checkbox checked={topic.is_active} disabled={activeMutation.isPending} aria-label={t('activeFor', { name: topic.name })} onCheckedChange={() => activeMutation.mutate(topic)} />
              <div className="min-w-0"><p className="font-medium">{topic.name}</p><p className="text-xs text-muted-foreground">{t('importanceValue', { value: topic.weight })}</p>{topic.description && <p className="text-sm text-muted-foreground">{topic.description}</p>}<p className="text-xs text-muted-foreground">{topic.keywords.join(', ')}</p></div>
            </div>
            <div className="flex gap-2"><Button type="button" variant="outline" onClick={() => beginEdit(topic)} disabled={isPending}>{t('edit')}</Button><Button type="button" variant="ghost" onClick={() => setDeleteTarget(topic)} disabled={isPending}>{t('delete')}</Button></div>
          </li>
        ))}
      </ul>
      {topicsQuery.isFetchNextPageError && <p role="alert" className="text-sm text-destructive">{t('loadMoreFailed')}</p>}
      {topicsQuery.hasNextPage && <div className="flex justify-center"><Button type="button" variant="outline" onClick={() => void topicsQuery.fetchNextPage()} disabled={topicsQuery.isFetchingNextPage}>{topicsQuery.isFetchingNextPage ? t('loadingMore') : topicsQuery.isFetchNextPageError ? t('retryLoadMore') : t('loadMore')}</Button></div>}
      {activeMutation.error && <p role="alert" className="text-sm text-destructive">{activeMutation.error instanceof ApiError && activeMutation.error.status === 409 ? t('conflict') : t('saveFailed')}</p>}

      {editing !== undefined && (
        <form className="flex flex-col gap-4 rounded-lg border border-border p-4" onSubmit={(event) => { event.preventDefault(); if (!isPending) saveMutation.mutate(); }}>
          <h3 className="font-medium">{editing ? t('editTitle') : t('createTitle')}</h3>
          <div className="grid gap-2"><Label htmlFor="topic-name">{t('name')}</Label><Input id="topic-name" maxLength={200} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} required /></div>
          <div className="grid gap-2"><Label htmlFor="topic-description">{t('descriptionField')}</Label><Input id="topic-description" maxLength={2000} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></div>
          <div className="grid gap-2"><Label htmlFor="topic-keywords">{t('keywords')}</Label><Input id="topic-keywords" value={draft.keywords} onChange={(event) => setDraft({ ...draft, keywords: event.target.value })} aria-describedby="topic-keyword-hint" /><p id="topic-keyword-hint" className="text-xs text-muted-foreground">{t('keywordBounds')}</p>{!keywordsInBounds && <p role="alert" className="text-sm text-destructive">{t('keywordBoundsError')}</p>}</div>
          <div className="grid gap-2"><Label htmlFor="topic-importance">{t('importance')}</Label><Input id="topic-importance" type="number" min="0" max="10" step="0.1" value={draft.weight} onChange={(event) => setDraft({ ...draft, weight: event.target.value })} required /><p className="text-xs text-muted-foreground">{t('importanceHint')}</p></div>
          <label className="flex items-center gap-2 text-sm"><Checkbox checked={draft.isActive} onCheckedChange={(checked) => setDraft({ ...draft, isActive: checked === true })} />{t('active')}</label>
          <div className="grid gap-2"><Label htmlFor="topic-entity-search">{t('entities')}</Label><Input id="topic-entity-search" value={entityQuery} onChange={(event) => setEntityQuery(event.target.value)} placeholder={t('searchEntities')} /><p className="text-xs text-muted-foreground">{t('entityBounds', { count: draft.entityIds.length })}</p>
            {draft.entityIds.length > 0 && <ul className="flex flex-wrap gap-2">{draft.entityIds.map((id) => <li key={id} className="flex items-center gap-1 rounded bg-secondary px-2 py-1 text-xs">{entitiesQuery.data?.items.find((item) => item.id === id)?.name ?? id}<Button type="button" variant="ghost" size="sm" aria-label={t('removeEntity', { id })} onClick={() => toggleEntity(id, false)}>×</Button></li>)}</ul>}
            <div className="max-h-40 overflow-y-auto rounded-md border p-2">{entitiesQuery.data?.items.map((entity) => <label key={entity.id} className="flex items-center gap-2 py-1 text-sm"><Checkbox checked={draft.entityIds.includes(entity.id)} disabled={!draft.entityIds.includes(entity.id) && draft.entityIds.length >= 100} onCheckedChange={(checked) => toggleEntity(entity.id, checked === true)} /><span>{entity.name ?? entity.id} <span className="text-muted-foreground">({entity.type})</span></span></label>)}{entitiesQuery.isLoading && <p role="status">{t('loading')}</p>}{entitiesQuery.error && <p role="alert">{t('entitiesFailed')}</p>}</div>
          </div>
          {saveMutation.error && conflictTopic && <div role="alert" className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
            <p>{t('conflict')}</p>
            <p>{t('draftRetained')}</p>
            {!conflictCurrent && <Button type="button" variant="outline" size="sm" onClick={() => void reloadConflict()} disabled={isPending}>{conflictReloadPending ? t('loadingCurrent') : t('reloadReview')}</Button>}
            {conflictReloadFailed && <p role="alert">{t('reloadFailed')}</p>}
            {conflictCurrent && <div className="mt-3 rounded border border-border bg-background p-3">
              <div className="grid gap-4 sm:grid-cols-2">
                <div><h4 className="font-medium">{t('currentServerVersion', { revision: conflictCurrent.revision })}</h4>
                  <dl className="mt-2 grid gap-1 text-xs">
                    <div><dt className="font-medium">{t('name')}</dt><dd>{conflictCurrent.name}</dd></div>
                    <div><dt className="font-medium">{t('descriptionField')}</dt><dd>{conflictCurrent.description || t('none')}</dd></div>
                    <div><dt className="font-medium">{t('keywords')}</dt><dd>{conflictCurrent.keywords.join(', ') || t('none')}</dd></div>
                    <div><dt className="font-medium">{t('importance')}</dt><dd>{conflictCurrent.weight}</dd></div>
                    <div><dt className="font-medium">{t('entities')}</dt><dd>{conflictCurrent.entity_ids.join(', ') || t('none')}</dd></div>
                    <div><dt className="font-medium">{t('active')}</dt><dd>{conflictCurrent.is_active ? t('yes') : t('no')}</dd></div>
                  </dl>
                </div>
                <div><h4 className="font-medium">{t('draftVersion')}</h4>
                  <dl className="mt-2 grid gap-1 text-xs">
                    <div><dt className="font-medium">{t('name')}</dt><dd>{draft.name || t('none')}</dd></div>
                    <div><dt className="font-medium">{t('descriptionField')}</dt><dd>{draft.description || t('none')}</dd></div>
                    <div><dt className="font-medium">{t('keywords')}</dt><dd>{draft.keywords || t('none')}</dd></div>
                    <div><dt className="font-medium">{t('importance')}</dt><dd>{draft.weight}</dd></div>
                    <div><dt className="font-medium">{t('entities')}</dt><dd>{draft.entityIds.join(', ') || t('none')}</dd></div>
                    <div><dt className="font-medium">{t('active')}</dt><dd>{draft.isActive ? t('yes') : t('no')}</dd></div>
                  </dl>
                </div>
              </div>
              {!conflictReviewed
                ? <Button type="button" className="mt-3" onClick={continueAfterConflictReview} disabled={isPending}>{t('continueAfterReview')}</Button>
                : <p className="mt-3" role="status">{t('reviewComplete', { revision: conflictCurrent.revision })}</p>}
            </div>}
          </div>}
          {saveMutation.error && (!conflictTopic || !(saveMutation.error instanceof ApiError && saveMutation.error.status === 409)) && <p role="alert" className="text-sm text-destructive">{t('saveFailed')}</p>}
          <div className="flex justify-end gap-2"><Button type="button" variant="outline" disabled={isPending} onClick={() => setEditing(undefined)}>{t('cancel')}</Button><Button type="submit" disabled={isPending || !draft.name.trim() || Number(draft.weight) < 0 || Number(draft.weight) > 10 || !Number.isFinite(Number(draft.weight)) || !keywordsInBounds || draft.entityIds.length > 100 || (!!conflictTopic && (!conflictCurrent || !conflictReviewed))}>{saveMutation.isPending ? t('saving') : t('save')}</Button></div>
        </form>
      )}

      <AlertDialog open={!!deleteTarget} onOpenChange={(open) => { if (!open && !deleteMutation.isPending) { setDeleteTarget(null); deleteMutation.reset(); } }}>
        <AlertDialogContent onEscapeKeyDown={(event) => { if (deleteMutation.isPending) event.preventDefault(); }}>
          <AlertDialogHeader><AlertDialogTitle>{t('deleteTitle')}</AlertDialogTitle><AlertDialogDescription>{t('deleteDescription', { name: deleteTarget?.name ?? '' })}</AlertDialogDescription></AlertDialogHeader>
          {deleteMutation.error && <p role="alert" className="text-sm text-destructive">{deleteMutation.error instanceof ApiError && deleteMutation.error.status === 409 ? t('conflict') : t('deleteFailed')}</p>}
          <AlertDialogFooter><AlertDialogCancel disabled={deleteMutation.isPending}>{t('cancel')}</AlertDialogCancel><Button variant="destructive" disabled={!deleteTarget || deleteMutation.isPending} onClick={() => deleteTarget && deleteMutation.mutate(deleteTarget)}>{deleteMutation.isPending ? t('deleting') : t('confirmDelete')}</Button></AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
