'use client';

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeftIcon } from 'lucide-react';
import Link from 'next/link';
import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import {
  acceptMemoryCandidate,
  createMemory,
  forgetMemory,
  listMemories,
  listMemoryCandidates,
  memoryKeys,
  rejectMemoryCandidate,
  updateMemory,
  type MemoryCandidate,
  type MemoryItem,
} from './api';

type MemoryFilterType = 'all' | 'fact' | 'preference' | 'instruction';

/** Labelled memory-type select shared by the creation form (fact, preference or instruction). */
function MemoryTypeField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const t = useTranslations('memory');
  return (
          <div className="space-y-1.5">
            <Label htmlFor="memory-type">{t('type')}</Label>
            <Select value={value} onValueChange={onChange}>
              <SelectTrigger id="memory-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="fact">{t('fact')}</SelectItem>
                <SelectItem value="preference">{t('preference')}</SelectItem>
                <SelectItem value="instruction">{t('instruction')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
  );
}

/**
 * Dialog for creating an explicit owner memory item.
 *
 * @param props.open Whether the creation dialog is visible.
 * @param props.onOpenChange Callback when dialog open state toggles.
 * @param props.csrfToken Authenticated session CSRF token.
 */
export function CreateMemoryDialog({
  open,
  onOpenChange,
  csrfToken,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  csrfToken: string;
}) {
  const t = useTranslations('memory');
  const queryClient = useQueryClient();
  const [content, setContent] = useState('');
  const [memoryType, setMemoryType] = useState('fact');
  const [reason, setReason] = useState('');

  const create = useMutation({
    mutationFn: () =>
      createMemory(
        {
          content: content.trim(),
          type: memoryType,
          reason: reason.trim() || undefined,
        },
        csrfToken,
      ),
    onSuccess: () => {
      setContent('');
      setReason('');
      setMemoryType('fact');
      onOpenChange(false);
      queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t('addMemory')}</DialogTitle>
          <DialogDescription>{t('subtitle')}</DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (content.trim()) create.mutate();
          }}
          className="space-y-4 pt-2"
        >
          <MemoryTypeField value={memoryType} onChange={setMemoryType} />

          <div className="space-y-1.5">
            <Label htmlFor="memory-content">{t('content')}</Label>
            <Input
              id="memory-content"
              placeholder={t('contentPlaceholder')}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              required
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="memory-reason">{t('reason')}</Label>
            <Input
              id="memory-reason"
              placeholder={t('reasonPlaceholder')}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </div>

          {create.error && (
            <p className="text-destructive text-sm" role="alert">
              {create.error instanceof ApiError ? create.error.message : t('errorLoading')}
            </p>
          )}

          <DialogFooter className="pt-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={create.isPending}
            >
              {t('cancel')}
            </Button>
            <Button type="submit" disabled={create.isPending || !content.trim()}>
              {create.isPending ? t('loading') : t('save')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Edits one memory's text through the PATCH route; only the content changes. */
function EditMemoryDialog({ item, onOpenChange, csrfToken }: { item: MemoryItem | null; onOpenChange: (open: boolean) => void; csrfToken: string }) {
  const t = useTranslations('memory');
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<{ id: string; text: string } | null>(null);
  const text = draft && item && draft.id === item.id ? draft.text : (item?.content ?? '');
  const save = useMutation({
    mutationFn: () => updateMemory(item!.id, { content: text.trim() }, csrfToken),
    onSuccess: () => {
      setDraft(null);
      onOpenChange(false);
      void queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });
  return (
    <Dialog open={item !== null} onOpenChange={(open) => { if (!save.isPending) { if (!open) { setDraft(null); save.reset(); } onOpenChange(open); } }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t('editMemory')}</DialogTitle>
          <DialogDescription>{t('memoryNote')}</DialogDescription>
        </DialogHeader>
        <form className="space-y-4 pt-2" onSubmit={(event) => { event.preventDefault(); if (text.trim() && item) save.mutate(); }}>
          <div className="space-y-1.5">
            <Label htmlFor="memory-edit-content">{t('content')}</Label>
            <Input id="memory-edit-content" value={text} required maxLength={2000} onChange={(event) => setDraft({ id: item?.id ?? '', text: event.target.value })} />
          </div>
          {save.error && <p className="text-destructive text-sm" role="alert">{save.error instanceof ApiError ? save.error.message : t('saveFailed')}</p>}
          <DialogFooter className="pt-2">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>{t('cancel')}</Button>
            <Button type="submit" disabled={save.isPending || !text.trim() || text.trim() === item?.content}>{save.isPending ? t('loading') : t('save')}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Renders the selective memory workspace panel with active memories, candidates,
 * manual creation, edit and a confirmed forget action.
 */
export function MemoryList() {
  const s0 = useMemoryListState();
  const { candidates, createDialogOpen, setCreateDialogOpen, editItem, setEditItem, csrfToken } = s0;
  const vm: MemoryListVm = s0;
  return (
    <section className="rounded-lg border bg-card p-6 space-y-6" aria-labelledby="memory-title">
<MemoryListHeader vm={vm} />

      {/* Filter and search toolbar */}
<MemoryListToolbar vm={vm} />

      {/* Active memories list */}
<ActiveMemories vm={vm} />

      {/* Memory candidates review section */}
      {candidates.length > 0 && (
<MemoryCandidatesReview vm={vm} />
      )}

      {/* Manual Memory Creation Dialog */}
      <CreateMemoryDialog
        open={createDialogOpen}
        onOpenChange={setCreateDialogOpen}
        csrfToken={csrfToken}
      />
      <EditMemoryDialog item={editItem} onOpenChange={(open) => { if (!open) setEditItem(null); }} csrfToken={csrfToken} />
<ForgetMemoryDialog vm={vm} />
    </section>
  );
}

type MemoryListVm = ReturnType<typeof useMemoryListState>;

/** Back link, title and the memory-settings / new-memory actions. */
function MemoryListHeader({ vm }: { vm: MemoryListVm }) {
  const { t, setCreateDialogOpen } = vm;
  return (
      <div className="section-heading flex flex-wrap items-start justify-between gap-4">
        <div className="grid gap-1">
          <Link href="/chat" className="inline-flex min-h-9 pointer-coarse:min-h-11 w-fit items-center gap-1 text-sm font-semibold text-primary underline-offset-4 hover:underline"><ArrowLeftIcon aria-hidden="true" className="size-4" />{t('backToChat')}</Link>
          <h1 id="memory-title" className="m-0 text-2xl font-semibold">{t('title')}</h1>
          <p className="text-muted-foreground text-sm text-muted-foreground">{t('subtitle')} {t('memoryNote')}</p>
        </div>
        <div className="form-actions">
          <Button asChild variant="outline"><Link href="/settings/memory">{t('memorySettings')}</Link></Button>
          <Button onClick={() => setCreateDialogOpen(true)}>{t('newMemory')}</Button>
        </div>
      </div>
  );
}

/** Kind filter tabs with counts and the search input. */
function MemoryListToolbar({ vm }: { vm: MemoryListVm }) {
  const { t, countFor, filterType, setFilterType, searchQuery, setSearchQuery } = vm;
  return (
      <div className="flex flex-wrap items-center gap-3">
        <Tabs value={filterType} onValueChange={(next) => setFilterType(next as MemoryFilterType)} className="min-w-0 max-w-full overflow-x-auto">
          <TabsList aria-label={t('filterByType')} className="h-auto w-max">
            {(['all', 'fact', 'preference', 'instruction'] as const).map((typeKey) => {
              const n = countFor(typeKey);
              return (
                <TabsTrigger key={typeKey} value={typeKey} className="min-h-10 gap-1.5 px-3 text-[13px] font-semibold">
                  {typeKey === 'all' ? t('filterAll') : t(typeKey)}
                  {n !== null && (
                    <>
                      <span className="tabular-nums" aria-hidden="true">{n}</span>
                      <span className="sr-only">{t('kindCount', { count: n })}</span>
                    </>
                  )}
                </TabsTrigger>
              );
            })}
          </TabsList>
        </Tabs>

        <div className="ms-auto w-full min-[720px]:w-[280px]">
          <Input
            type="search"
            className="min-h-9 pointer-coarse:min-h-11"
            aria-label={t('searchMemories')}
            placeholder={t('searchMemories')}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
      </div>
  );
}

/** Loading, error, empty and populated states of the active memory list. */
function ActiveMemories({ vm }: { vm: MemoryListVm }) {
  const { t, display, memoriesQuery, items, setEditItem, setForgetItem } = vm;
  return (
      <div className="space-y-3">
        {memoriesQuery.isPending && (
          <div className="skeleton h-32 rounded-lg" role="status" aria-label={t('loading')} />
        )}

        {memoriesQuery.isError && (
          <div className="flex items-center justify-between rounded-md border border-destructive/20 bg-destructive/10 p-4 text-sm text-destructive" role="alert">
            <span>{t('errorLoading')}</span>
            <Button
              variant="outline"
              onClick={() => memoriesQuery.refetch()}
            >
              {t('retry')}
            </Button>
          </div>
        )}

        {memoriesQuery.isSuccess && items.length === 0 && (
          <div className="empty-state rounded-lg border border-dashed border-border bg-card p-8 text-center">
            <h3 className="mb-1 text-base font-semibold">{t('emptyTitle')}</h3>
            <p className="mx-auto max-w-md text-sm text-muted-foreground">
              {t('emptyDescription')}
            </p>
          </div>
        )}

        {items.length > 0 && (
          <ul className="record-list m-0 divide-y divide-border overflow-hidden rounded-lg border bg-card">
            {items.map((item: MemoryItem) => (
              <li
                key={item.id}
                className="flex flex-wrap items-start justify-between gap-4 p-4 transition-colors hover:bg-secondary"
              >
                <div className="min-w-0 flex-1 space-y-1.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline">{t(item.type as 'fact' | 'preference' | 'instruction')}</Badge>
                    {item.confidence < 1.0 && (
                      <span className="text-xs text-muted-foreground">
                        {Math.round(item.confidence * 100)}% {t('confidence')}
                      </span>
                    )}
                  </div>
                  <p className="break-words text-sm font-medium text-foreground">{item.content}</p>
                  {item.reason && (
                    <p className="text-xs italic text-muted-foreground">{item.reason}</p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    {item.is_manual ? t('addedByYou') : t('model')} · {formatDateTime(item.created_at, display.locale, display.timezone)}
                  </p>
                </div>

                <div className="flex shrink-0 items-center gap-2">
                  <Button variant="ghost" onClick={() => setEditItem(item)}>{t('edit')}</Button>
                  <Button
                    variant="ghost"
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                    onClick={() => setForgetItem(item)}
                  >
                    {t('forget')}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
  );
}

/** Pending memory candidates with accept and reject actions. */
function MemoryCandidatesReview({ vm }: { vm: MemoryListVm }) {
  const { t, acceptCandidateMutation, rejectCandidateMutation, candidates } = vm;
  return (
        <div className="space-y-3 border-t border-border pt-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">{t('candidatesTitle')}</h2>
            <span className="text-xs text-muted-foreground">
              {candidates.length} {t('candidatesTitle').toLowerCase()}
            </span>
          </div>

          <ul className="record-list divide-y divide-border overflow-hidden rounded-lg border bg-background">
            {candidates.map((cand: MemoryCandidate) => (
              <li
                key={cand.id}
                className="flex flex-wrap items-start justify-between gap-4 p-4 transition-colors hover:bg-secondary"
              >
                <div className="min-w-0 flex-1 space-y-1.5">
                  <div className="flex items-center gap-2">
                    <Badge variant="outline">{t(cand.type as 'fact' | 'preference' | 'instruction')}</Badge>
                    <span className="text-xs text-muted-foreground">
                      {Math.round(cand.confidence * 100)}% {t('confidence')}
                    </span>
                  </div>
                  <p className="break-words text-sm font-medium text-foreground">{cand.content}</p>
                  {cand.reason && (
                    <p className="text-xs text-muted-foreground">{cand.reason}</p>
                  )}
                </div>

                <div className="flex shrink-0 items-center gap-2">
                  <Button
                    disabled={acceptCandidateMutation.isPending}
                    onClick={() => acceptCandidateMutation.mutate(cand.id)}
                  >
                    {t('accept')}
                  </Button>
                  <Button
                    variant="outline"
                    disabled={rejectCandidateMutation.isPending}
                    onClick={() => rejectCandidateMutation.mutate(cand.id)}
                  >
                    {t('reject')}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </div>
  );
}

/** Confirmation dialog that forgets the selected memory. */
function ForgetMemoryDialog({ vm }: { vm: MemoryListVm }) {
  const { t, forgetMutation, forgetItem, setForgetItem } = vm;
  return (
      <Dialog open={forgetItem !== null} onOpenChange={(open) => { if (!open && !forgetMutation.isPending) { setForgetItem(null); forgetMutation.reset(); } }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t('forgetName')}</DialogTitle>
            <DialogDescription>{t('forgetConfirm')}</DialogDescription>
          </DialogHeader>
          {forgetItem && <p className="break-words rounded-md border border-border p-3 text-sm">{forgetItem.content}</p>}
          {forgetMutation.error && <p className="text-destructive text-sm" role="alert">{forgetMutation.error instanceof ApiError ? forgetMutation.error.message : t('forgetFailed')}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={forgetMutation.isPending} onClick={() => setForgetItem(null)}>{t('cancel')}</Button>
            <Button type="button" variant="destructive" disabled={forgetMutation.isPending} onClick={() => { if (forgetItem) forgetMutation.mutate(forgetItem.id); }}>{forgetMutation.isPending ? t('loading') : t('forget')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
  );
}

/** Loads the active memories, per-kind counts and pending candidates (search debounced 300 ms) and declares the forget/accept/reject mutations. */
function useMemoryListState() {
  const t = useTranslations('memory');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const display = useDisplayPreferences();
  const [filterType, setFilterType] = useState<MemoryFilterType>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editItem, setEditItem] = useState<MemoryItem | null>(null);
  const [forgetItem, setForgetItem] = useState<MemoryItem | null>(null);

  const [debouncedQuery, setDebouncedQuery] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(searchQuery), 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  const queryType = filterType === 'all' ? undefined : filterType;
  const q = debouncedQuery || undefined;
  const memoriesQuery = useQuery({
    queryKey: memoryKeys.list('active', queryType, q),
    queryFn: () => listMemories(undefined, queryType, 'active', q),
    placeholderData: keepPreviousData,
  });
  // Counts are per kind and the server only computes them for unfiltered pages: this query is shared with the list
  // when the All chip is active, so switching type chips never triggers a recount.
  const countsQuery = useQuery({
    queryKey: memoryKeys.list('active', undefined, q),
    queryFn: () => listMemories(undefined, undefined, 'active', q),
    placeholderData: keepPreviousData,
  });

  const candidatesQuery = useQuery({
    queryKey: memoryKeys.candidates('pending'),
    queryFn: () => listMemoryCandidates(undefined, 'pending'),
  });

  const forgetMutation = useMutation({
    mutationFn: (id: string) => forgetMemory(id, undefined, csrfToken),
    onSuccess: () => {
      setForgetItem(null);
      void queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });

  const acceptCandidateMutation = useMutation({
    mutationFn: (id: string) => acceptMemoryCandidate(id, csrfToken),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });

  const rejectCandidateMutation = useMutation({
    mutationFn: (id: string) => rejectMemoryCandidate(id, undefined, csrfToken),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });

  const items = memoriesQuery.data?.items ?? [];
  const candidates = candidatesQuery.data?.items ?? [];
  // Counts cover only rows the list itself would show (server verifies each row). Past the cap (or while searching) the server sends
  // no numbers; counts_capped marks the cap case; "All" also includes decision/procedural kinds that have no chip.
  const kindCounts = countsQuery.data?.kind_counts ?? null;
  /** Counts memories of the given filter type. */
  const countFor = (key: MemoryFilterType) =>
    kindCounts ? (key === 'all' ? Object.values(kindCounts).reduce((a, b) => a + b, 0) : (kindCounts[key] ?? 0)) : null;


  return { t, queryClient, display, queryType, q, memoriesQuery, countsQuery, candidatesQuery, forgetMutation, acceptCandidateMutation, rejectCandidateMutation, items, candidates, kindCounts, countFor, filterType, setFilterType, searchQuery, setSearchQuery, createDialogOpen, setCreateDialogOpen, editItem, setEditItem, forgetItem, setForgetItem, debouncedQuery, setDebouncedQuery, csrfToken };
}
