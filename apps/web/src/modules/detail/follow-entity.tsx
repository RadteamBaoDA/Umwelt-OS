'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Plus } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { Button } from '@/components/ui/button';
import { createTopic, fetchAllTopics, updateTopic } from '@/modules/news/api';

const topicsKey = ['topics', 'follow-lookup'] as const;

/**
 * Follow = a topic whose entity_ids contain this entity. Idempotent: an active linked topic means
 * "Following" and the button is disabled; an inactive linked topic is reactivated rather than duplicated.
 */
export function FollowEntityButton({ entityId, name }: { entityId: string; name: string | null }) {
  const t = useTranslations('detail');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const topics = useQuery({ queryKey: topicsKey, queryFn: ({ signal }) => fetchAllTopics({ signal }) });
  const linked = topics.data?.filter((topic) => topic.entity_ids.includes(entityId)) ?? [];
  const following = linked.some((topic) => topic.is_active);
  const follow = useMutation({
    mutationFn: async () => {
      const inactive = linked[0];
      if (inactive) return updateTopic(inactive.id, { expected_revision: inactive.revision, is_active: true }, csrfToken);
      return createTopic({ name: (name ?? '').trim().slice(0, 200), entity_ids: [entityId] }, csrfToken);
    },
    // A conflict means another write won; refetch so the button reflects the real state.
    onSettled: () => Promise.all([queryClient.invalidateQueries({ queryKey: ['topics'] }), queryClient.invalidateQueries({ queryKey: ['news-stories'] })]),
  });
  // aria-disabled (not disabled) keeps the button focusable so the reason stays reachable.
  const blocked = topics.isPending || follow.isPending || following || !name;
  if (topics.isError) return <p role="alert" className="text-xs text-destructive">{t('followLoadFailed')} <Button type="button" variant="outline" size="sm" onClick={() => { void topics.refetch(); }}>{t('retry')}</Button></p>;
  return <div className="flex flex-col items-start gap-1">
    <Button type="button" variant={following ? 'secondary' : 'outline'} aria-pressed={following}
      aria-disabled={blocked}
      aria-describedby={!name ? 'follow-reason' : undefined}
      onClick={() => { if (!blocked) follow.mutate(); }}>
      {following ? <Check aria-hidden="true" className="size-4" /> : <Plus aria-hidden="true" className="size-4" />}
      {follow.isPending ? t('followPending') : following ? t('following') : t('follow')}
    </Button>
    {!name ? <p id="follow-reason" className="text-xs text-muted-foreground">{t('followNeedsName')}</p> : null}
    {follow.isError ? <p role="alert" className="text-xs text-destructive">{follow.error instanceof ApiError && follow.error.status === 409 ? t('followConflict') : t('followFailed')}</p> : null}
  </div>;
}
