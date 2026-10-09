'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { createTopic, fetchAllTopics, updateTopic } from '@/modules/news/api';
import type { Topic } from '@/modules/news/types';

/** Stable English topic names: the stored name is the identity so re-running onboarding in any locale never duplicates. */
const PRESETS = [
  { id: 'Economy', vi: 'kinh tế', label: 'topicEconomy' },
  { id: 'Technology', vi: 'công nghệ', label: 'topicTechnology' },
  { id: 'Health', vi: 'sức khỏe', label: 'topicHealth' },
  { id: 'Climate', vi: 'khí hậu', label: 'topicClimate' },
  { id: 'Science', vi: 'khoa học', label: 'topicScience' },
  { id: 'Politics', vi: 'chính trị', label: 'topicPolitics' },
  { id: 'Sports', vi: 'thể thao', label: 'topicSports' },
  { id: 'Culture', vi: 'văn hóa', label: 'topicCulture' },
] as const;
const key = ['topics', 'onboarding'] as const;

/** Chip-owned topics are name matches without entity links; entity Follow topics (entity_ids set) are never touched. */
const ownsPreset = (topic: Topic, id: string) => topic.entity_ids.length === 0 && topic.name.toLowerCase() === id.toLowerCase();

/** Topic chips: select = reactivate-or-create, deselect = deactivate (never deletes user data). */
export function OnboardingTopics() {
  const t = useTranslations('onboarding');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const topics = useQuery({ queryKey: key, queryFn: ({ signal }) => fetchAllTopics({ signal }) });
  const toggle = useMutation({
    mutationFn: async (preset: (typeof PRESETS)[number]) => {
      const matches = topics.data?.filter((topic) => ownsPreset(topic, preset.id)) ?? [];
      const active = matches.filter((topic) => topic.is_active);
      // Deselect clears exactly what `pressed` reflects; select reactivates before it creates.
      if (active.length) return Promise.all(active.map((topic) => updateTopic(topic.id, { expected_revision: topic.revision, is_active: false }, csrfToken)));
      if (matches[0]) return updateTopic(matches[0].id, { expected_revision: matches[0].revision, is_active: true }, csrfToken);
      return createTopic({ name: preset.id, keywords: [preset.id.toLowerCase(), preset.vi] }, csrfToken);
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
        const pressed = topics.data.some((topic) => ownsPreset(topic, preset.id) && topic.is_active);
        return <Button key={preset.id} type="button" size="sm" className="min-h-11" variant={pressed ? 'default' : 'outline'} aria-pressed={pressed}
          disabled={toggle.isPending} onClick={() => toggle.mutate(preset)}>{t(preset.label)}</Button>;
      })}
    </div> : null}
    {toggle.isError ? <p role="alert" className="error">{t('topicsSaveFailed')}</p> : null}
  </div>;
}
