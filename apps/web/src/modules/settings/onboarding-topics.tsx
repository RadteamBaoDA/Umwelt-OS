'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { createTopic, fetchTopics, updateTopic } from '@/modules/news/api';
import type { Topic } from '@/modules/news/types';

/** Stable English topic names: the stored name is the identity so re-running onboarding in any locale never duplicates. */
const PRESETS = [
  { id: 'Economy', label: 'topicEconomy' },
  { id: 'Technology', label: 'topicTechnology' },
  { id: 'Health', label: 'topicHealth' },
  { id: 'Climate', label: 'topicClimate' },
  { id: 'Science', label: 'topicScience' },
  { id: 'Politics', label: 'topicPolitics' },
  { id: 'Sports', label: 'topicSports' },
  { id: 'Culture', label: 'topicCulture' },
] as const;
const MAX_PAGES = 10;
const key = ['topics', 'onboarding'] as const;

/** Reads every owner topic (bounded) so selected state never depends on one page. */
async function fetchAll(): Promise<Topic[]> {
  const items: Topic[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await fetchTopics({ limit: 100, cursor });
    items.push(...result.items);
    if (!result.next_cursor) break;
    cursor = result.next_cursor;
  }
  return items;
}

/** Topic chips: select = reactivate-or-create, deselect = deactivate (never deletes user data). */
export function OnboardingTopics() {
  const t = useTranslations('onboarding');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const topics = useQuery({ queryKey: key, queryFn: fetchAll });
  const toggle = useMutation({
    mutationFn: async (name: string) => {
      const existing = topics.data?.find((topic) => topic.name.toLowerCase() === name.toLowerCase());
      if (!existing) return createTopic({ name, keywords: [name.toLowerCase()] }, csrfToken);
      return updateTopic(existing.id, { expected_revision: existing.revision, is_active: !existing.is_active }, csrfToken);
    },
    // Refetch on success or conflict so chips always show server state.
    onSettled: () => Promise.all([client.invalidateQueries({ queryKey: ['topics'] }), client.invalidateQueries({ queryKey: ['news-stories'] })]),
  });
  return <div className="space-y-2">
    <h3 className="text-sm font-semibold">{t('topicsTitle')}</h3>
    <p className="muted text-sm">{t('topicsText')}</p>
    {topics.isPending ? <p role="status" className="muted text-sm">{t('topicsLoading')}</p> : null}
    {topics.isError ? <p role="alert" className="error">{t('topicsLoadFailed')} <Button type="button" variant="outline" size="sm" onClick={() => { void topics.refetch(); }}>{t('retry')}</Button></p> : null}
    {topics.data ? <div className="flex flex-wrap gap-2" role="group" aria-label={t('topicsTitle')}>
      {PRESETS.map((preset) => {
        const pressed = topics.data.some((topic) => topic.name.toLowerCase() === preset.id.toLowerCase() && topic.is_active);
        return <Button key={preset.id} type="button" size="sm" className="min-h-11" variant={pressed ? 'default' : 'outline'} aria-pressed={pressed}
          disabled={toggle.isPending} onClick={() => toggle.mutate(preset.id)}>{t(preset.label)}</Button>;
      })}
    </div> : null}
    {toggle.isError ? <p role="alert" className="error">{t('topicsSaveFailed')}</p> : null}
  </div>;
}
