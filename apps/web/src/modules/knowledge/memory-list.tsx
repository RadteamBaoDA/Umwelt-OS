'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
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
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import {
  acceptMemoryCandidate,
  createMemory,
  forgetMemory,
  listMemories,
  listMemoryCandidates,
  memoryKeys,
  rejectMemoryCandidate,
  type MemoryCandidate,
  type MemoryItem,
} from './api';

type MemoryFilterType = 'all' | 'fact' | 'preference' | 'instruction';

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
          <div className="space-y-1.5">
            <Label htmlFor="memory-type">{t('type')}</Label>
            <Select value={memoryType} onValueChange={setMemoryType}>
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
              className="secondary"
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

/**
 * Renders the selective memory workspace panel with active memories, candidates,
 * manual creation, and immediate forget action.
 */
export function MemoryList() {
  const t = useTranslations('memory');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [filterType, setFilterType] = useState<MemoryFilterType>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [forgetId, setForgetId] = useState<string | null>(null);

  const queryType = filterType === 'all' ? undefined : filterType;
  const memoriesQuery = useQuery({
    queryKey: memoryKeys.list('active', queryType, searchQuery || undefined),
    queryFn: () => listMemories(undefined, queryType, 'active', searchQuery || undefined),
  });

  const candidatesQuery = useQuery({
    queryKey: memoryKeys.candidates('pending'),
    queryFn: () => listMemoryCandidates(undefined, 'pending'),
  });

  const forgetMutation = useMutation({
    mutationFn: (id: string) => forgetMemory(id, undefined, csrfToken),
    onSuccess: () => {
      setForgetId(null);
      queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });

  const acceptCandidateMutation = useMutation({
    mutationFn: (id: string) => acceptMemoryCandidate(id, csrfToken),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });

  const rejectCandidateMutation = useMutation({
    mutationFn: (id: string) => rejectMemoryCandidate(id, undefined, csrfToken),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });

  const items = memoriesQuery.data?.items ?? [];
  const candidates = candidatesQuery.data?.items ?? [];

  return (
    <section className="content-panel space-y-6">
      <div className="section-heading flex items-start justify-between gap-4">
        <div>
          <span className="brand">{t('activeMemories')}</span>
          <h1 className="text-2xl font-bold tracking-tight">{t('title')}</h1>
          <p className="muted text-sm text-muted-foreground">{t('subtitle')}</p>
        </div>
        <div className="form-actions">
          <Button onClick={() => setCreateDialogOpen(true)}>{t('newMemory')}</Button>
        </div>
      </div>

      {/* Filter and search toolbar */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex rounded-md border border-input p-0.5 bg-background">
          {(['all', 'fact', 'preference', 'instruction'] as const).map((typeKey) => (
            <button
              key={typeKey}
              type="button"
              onClick={() => setFilterType(typeKey)}
              className={`px-3 py-1.5 text-xs font-medium rounded-sm transition-colors ${
                filterType === typeKey
                  ? 'bg-primary text-primary-foreground shadow-sm'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              {typeKey === 'all' ? t('filterAll') : t(typeKey)}
            </button>
          ))}
        </div>

        <div className="flex-1 min-w-[200px]">
          <Input
            placeholder={t('contentPlaceholder')}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="h-8 text-xs"
          />
        </div>
      </div>

      {/* Active memories list */}
      <div className="space-y-3">
        {memoriesQuery.isPending && (
          <div className="skeleton h-32 rounded-lg" aria-label={t('loading')} />
        )}

        {memoriesQuery.isError && (
          <div className="p-4 rounded-md border border-destructive/20 bg-destructive/10 text-destructive text-sm flex items-center justify-between">
            <span>{t('errorLoading')}</span>
            <Button
              className="secondary"
              onClick={() => memoriesQuery.refetch()}
            >
              {t('retry')}
            </Button>
          </div>
        )}

        {memoriesQuery.isSuccess && items.length === 0 && (
          <div className="empty-state p-8 text-center rounded-lg border border-dashed border-border">
            <h3 className="font-semibold text-base mb-1">{t('emptyTitle')}</h3>
            <p className="text-sm text-muted-foreground max-w-md mx-auto mb-4">
              {t('emptyDescription')}
            </p>
            <Button onClick={() => setCreateDialogOpen(true)}>
              {t('addMemory')}
            </Button>
          </div>
        )}

        {items.length > 0 && (
          <ul className="record-list divide-y divide-border border rounded-lg overflow-hidden bg-card">
            {items.map((item: MemoryItem) => (
              <li
                key={item.id}
                className="p-4 flex items-start justify-between gap-4 hover:bg-muted/40 transition-colors"
              >
                <div className="space-y-1.5 min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-secondary text-secondary-foreground">
                      {t(item.type as 'fact' | 'preference' | 'instruction')}
                    </span>
                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-muted text-muted-foreground">
                      {item.is_manual ? t('manual') : t('model')}
                    </span>
                    {item.confidence < 1.0 && (
                      <span className="text-xs text-muted-foreground">
                        {Math.round(item.confidence * 100)}% {t('confidence')}
                      </span>
                    )}
                  </div>
                  <p className="font-medium text-sm text-foreground break-words">{item.content}</p>
                  {item.reason && (
                    <p className="text-xs text-muted-foreground italic">{item.reason}</p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    {new Date(item.created_at).toLocaleString()}
                  </p>
                </div>

                <div className="flex items-center gap-2 shrink-0">
                  <Button
                    className="secondary text-destructive hover:bg-destructive/10 hover:text-destructive border-destructive/30"
                    disabled={forgetMutation.isPending && forgetId === item.id}
                    onClick={() => {
                      setForgetId(item.id);
                      forgetMutation.mutate(item.id);
                    }}
                  >
                    {t('forget')}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Memory candidates review section */}
      {candidates.length > 0 && (
        <div className="space-y-3 pt-4 border-t border-border">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">{t('candidatesTitle')}</h2>
            <span className="text-xs text-muted-foreground">
              {candidates.length} {t('candidatesTitle').toLowerCase()}
            </span>
          </div>

          <ul className="record-list divide-y divide-border border rounded-lg overflow-hidden bg-card">
            {candidates.map((cand: MemoryCandidate) => (
              <li
                key={cand.id}
                className="p-4 flex items-start justify-between gap-4 hover:bg-muted/40 transition-colors"
              >
                <div className="space-y-1.5 min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-primary/10 text-primary">
                      {t(cand.type as 'fact' | 'preference' | 'instruction')}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {Math.round(cand.confidence * 100)}% {t('confidence')}
                    </span>
                  </div>
                  <p className="font-medium text-sm text-foreground break-words">{cand.content}</p>
                  {cand.reason && (
                    <p className="text-xs text-muted-foreground">{cand.reason}</p>
                  )}
                </div>

                <div className="flex items-center gap-2 shrink-0">
                  <Button
                    disabled={acceptCandidateMutation.isPending}
                    onClick={() => acceptCandidateMutation.mutate(cand.id)}
                  >
                    {t('accept')}
                  </Button>
                  <Button
                    className="secondary"
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
      )}

      {/* Manual Memory Creation Dialog */}
      <CreateMemoryDialog
        open={createDialogOpen}
        onOpenChange={setCreateDialogOpen}
        csrfToken={csrfToken}
      />
    </section>
  );
}
