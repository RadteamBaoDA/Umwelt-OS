'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useWorkspaceSession } from '@/core/app-shell/workspace-session';
import { BriefTaskEditor, ScheduleText } from '@/modules/automations';
import { briefCron, listAutomations, scheduledTaskKind } from '@/modules/automations/public-api';
import type { components } from '@/core/api-types.gen';

type ScheduledTaskDraft = components['schemas']['ScheduledTaskDraft'];

/** Literal catalog keys for the section labels (reuses the automations catalog). */
const SECTION_KEYS = {
  stories: 'scheduledTasks.section.stories',
  markets: 'scheduledTasks.section.markets',
  weather: 'scheduledTasks.section.weather',
  hazards: 'scheduledTasks.section.hazards',
  agenda: 'scheduledTasks.section.agenda',
  source_health: 'scheduledTasks.section.source_health',
} as const;

/**
 * Card for a scheduled task the assistant proposed (`draft_scheduled_task`). Shows the draft as "Not saved yet".
 * "Review and create" opens the Daily brief editor pre-filled with the draft; the owner's Save there is the only
 * write (the normal create endpoint, with CSRF). This component makes no request of its own except the automations
 * list read, used to show "already exists" when the workspace has a Daily brief task. All text is rendered as plain
 * React text (the draft is model output).
 *
 * @param props.draft - Validated draft from `MessageRead.task_draft`.
 */
export function TaskDraftCard({ draft }: { draft: ScheduledTaskDraft }) {
  const t = useTranslations('chat');
  const ta = useTranslations('automations');
  const { csrfToken } = useWorkspaceSession();
  const [editing, setEditing] = useState(false);
  const [created, setCreated] = useState(false);
  const automations = useQuery({ queryKey: ['automations'], queryFn: listAutomations });
  const existing = automations.data?.items.find((rule) => scheduledTaskKind(rule) === 'daily_brief');
  const cron = briefCron(draft.time, draft.days);
  const sections = draft.sections ?? [];
  const linkId = existing?.id;

  return (
    <Card className="mt-3" data-testid="task-draft-card">
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <CardTitle className="text-sm">{t('taskDraft.title')}</CardTitle>
        {!created && !existing && <Badge variant="outline">{t('taskDraft.notSaved')}</Badge>}
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <p className="font-medium">{draft.name}</p>
        <p className="text-muted-foreground">
          <ScheduleText cron={cron} />
        </p>
        <p className="text-muted-foreground">{sections.map((section) => ta(SECTION_KEYS[section])).join(', ')}</p>
        {draft.extra_instruction ? <p className="text-muted-foreground">{draft.extra_instruction}</p> : null}
        {created || existing ? (
          <div className="flex items-center gap-3">
            <span>{created ? t('taskDraft.created') : t('taskDraft.exists')}</span>
            <Link
              className="text-accent underline"
              href={linkId ? `/settings/scheduled-tasks?edit=${linkId}` : '/settings/scheduled-tasks'}
            >
              {t('taskDraft.open')}
            </Link>
          </div>
        ) : (
          <Button type="button" size="sm" onClick={() => setEditing(true)}>
            {t('taskDraft.review')}
          </Button>
        )}
      </CardContent>
      {editing && (
        <BriefTaskEditor
          csrfToken={csrfToken}
          initial={{
            time: draft.time,
            days: draft.days,
            sections,
            extraInstruction: draft.extra_instruction ?? null,
            modelRole: draft.model_role,
          }}
          onSaved={() => setCreated(true)}
          onClose={() => setEditing(false)}
        />
      )}
    </Card>
  );
}
