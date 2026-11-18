'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query';
import {
  ClockIcon,
  HistoryIcon,
  LayoutDashboardIcon,
  MessageSquareIcon,
  PauseIcon,
  PencilIcon,
  PlayIcon,
  SunIcon,
  TelescopeIcon,
  Trash2Icon,
} from 'lucide-react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useWorkspaceSession } from '@/core/app-shell/workspace-session';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import {
  errorMessageKey,
  getLastRun,
  getRuleConversationId,
  listAutomations,
  patchAutomation,
  runAutomation,
  type Action,
  type Automation,
  type Run,
} from './api';
import { BriefTaskEditor } from './brief-task-editor';
import { runStatusLabel } from './message-keys';
import { RuleEditor } from './rule-editor';
import { RunDetail } from './run-detail';
import {
  TEMPLATES,
  describeSchedule,
  isLegacyBrief,
  scheduledTaskKind,
  topicOutcome,
  type ScheduledTaskTemplate,
} from './scheduled-task-model';
import { TopicWatchDeleteDialog } from './topic-watch-delete-dialog';
import { TopicWatchEditor } from './topic-watch-editor';

/** Literal catalog keys of the Topic watch run outcomes. */
const OUTCOME_KEYS = {
  collecting: 'scheduledTasks.topic.outcome.collecting',
  settled: 'scheduledTasks.topic.outcome.settled',
  no_new_data: 'scheduledTasks.topic.outcome.noNew',
  failed: 'scheduledTasks.topic.outcome.failed',
} as const;

type Editor =
  { mode: 'new'; kind: 'daily_brief' | 'topic_watch' } | { mode: 'edit'; rule: Automation; consentStale?: boolean } | { mode: 'convert' };
type View = { kind: 'list' } | { kind: 'blank' } | { kind: 'runs'; ruleId: string };

/**
 * Templates grid. The Daily brief card is disabled with "already set up" when a task exists, and with a convert link
 * when a legacy brief rule owns the slot; the Topic watch card is never disabled (many watches are allowed). `onUse`
 * opens the editor of the chosen template.
 */
function TemplateGrid({
  hasTask,
  hasLegacy,
  onUse,
  onConvert,
}: {
  hasTask: boolean;
  hasLegacy: boolean;
  onUse: (template: ScheduledTaskTemplate) => void;
  onConvert: () => void;
}) {
  const t = useTranslations('automations');
  return (
    <ul className="grid gap-3 sm:grid-cols-2">
      {TEMPLATES.map((template) => {
        const brief = template.kind === 'daily_brief';
        const blocked = brief && (hasTask || hasLegacy);
        return (
          <li key={template.id} className="space-y-3 rounded-lg border border-border bg-surface p-4">
            <div className="flex items-center gap-2 font-semibold">
              {brief ? <SunIcon size={16} aria-hidden /> : <TelescopeIcon size={16} aria-hidden />}
              {brief ? t('scheduledTasks.templateBriefTitle') : t('scheduledTasks.templateTopicTitle')}
            </div>
            <p className="text-sm text-muted-foreground">
              {brief ? t('scheduledTasks.templateBriefBody') : t('scheduledTasks.templateTopicBody')}
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <Button type="button" size="sm" disabled={blocked} onClick={() => onUse(template)}>
                {brief && hasTask ? t('scheduledTasks.alreadySetUp') : t('scheduledTasks.useTemplate')}
              </Button>
              {brief && hasLegacy && !hasTask && (
                <Button type="button" variant="link" size="sm" onClick={onConvert}>
                  {t('scheduledTasks.convertExisting')}
                </Button>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** True when the editor state targets a Topic watch (a new one from its template, or an existing watch being edited). */
function isTopicEditor(editor: Editor): boolean {
  if (editor.mode === 'edit') return scheduledTaskKind(editor.rule) === 'topic_watch';
  return editor.mode === 'new' && editor.kind === 'topic_watch';
}

/**
 * Plain-language schedule for the two generated cron patterns (localized); any other cron is shown raw.
 *
 * @param props.cron - Cron expression of the task's schedule trigger.
 */
export function ScheduleText({ cron }: { cron: string }) {
  const t = useTranslations('automations');
  const schedule = describeSchedule(cron);
  if (!schedule) return <>{cron}</>;
  return (
    <>
      {schedule.days === 'daily'
        ? t('scheduledTasks.schedule.daily', { time: schedule.time })
        : t('scheduledTasks.schedule.weekdays', { time: schedule.time })}
    </>
  );
}

/**
 * True when the newest run of a Topic watch was skipped for `consent_required` at the rule's current revision, i.e. the
 * stored consent no longer matches the definition. Re-consenting bumps the revision, which clears it at once.
 */
function isConsentStale(rule: Automation, run: Run | undefined): boolean {
  const outcome = run?.actions[0] ? topicOutcome(run.actions[0]) : null;
  return outcome === 'consent_required' && run?.revision === rule.revision;
}

/**
 * Last-run outcome of a Topic watch under its summary: collected, collecting, no new items, failed, or a "Needs
 * consent" badge button that opens the editor to re-grant consent. Renders nothing when the run has no known outcome.
 */
function TopicRunLine({ run, action, stale, onEdit }: { run: Run; action: Action | undefined; stale: boolean; onEdit: () => void }) {
  const t = useTranslations('automations');
  const outcome = run.actions[0] ? topicOutcome(run.actions[0]) : null;
  const needsConsent = !action?.consent || stale;
  return (
    <div className="text-xs">
      {needsConsent && (
        <button type="button" className="rounded border border-border px-1.5 py-0.5" onClick={onEdit}>
          {t('scheduledTasks.topic.needsConsent')}
        </button>
      )}
      {!needsConsent && outcome && outcome !== 'consent_required' && (
        <span className="text-muted-foreground">{t(OUTCOME_KEYS[outcome])}</span>
      )}
    </div>
  );
}

/**
 * Task cell under the name: the first line of the topic (Topic watch) or the extra instruction / section count
 * (Daily brief); for a Topic watch also its kind and last-run outcome.
 */
function TaskSummary({ rule, run, stale, onEdit }: { rule: Automation; run: Run | undefined; stale: boolean; onEdit: () => void }) {
  const t = useTranslations('automations');
  const action = rule.actions[0];
  const topic = scheduledTaskKind(rule) === 'topic_watch';
  const firstLine = (topic ? action?.watch?.topic : action?.extra_instruction)?.split(/\r?\n/)[0];
  return (
    <>
      <div className="text-xs text-muted-foreground">
        {firstLine || t('scheduledTasks.briefSummary', { count: action?.sections?.length ?? 0 })}
      </div>
      {topic && <div className="text-xs text-muted-foreground">{t('scheduledTasks.topic.kind')}</div>}
      {topic && run && <TopicRunLine run={run} action={action} stale={stale} onEdit={onEdit} />}
    </>
  );
}

/**
 * Topic-watch-only row actions: "Show on dashboard" (links to the Feed template with the task's source preselected;
 * hidden until the server has assigned a source) and Delete (opens the keep/purge dialog). Renders nothing for a brief.
 */
function TopicActions({
  rule,
  label,
  onDelete,
}: {
  rule: Automation;
  label: (key: string, text: string, handler: () => void, icon: React.ReactNode) => React.ReactNode;
  onDelete: () => void;
}) {
  const t = useTranslations('automations');
  if (scheduledTaskKind(rule) !== 'topic_watch') return null;
  const sourceId = rule.actions[0]?.source_id;
  return (
    <>
      {sourceId && (
        <Button asChild variant="ghost" size="icon" title={t('scheduledTasks.topic.showOnDashboard')}>
          <Link
            href={`/dashboard?addTemplate=feed&source=${encodeURIComponent(sourceId)}`}
            aria-label={t('scheduledTasks.topic.showOnDashboard')}
          >
            <LayoutDashboardIcon size={16} aria-hidden />
          </Link>
        </Button>
      )}
      {label('delete', t('scheduledTasks.topic.delete'), onDelete, <Trash2Icon size={16} aria-hidden />)}
    </>
  );
}

/** One task row; loads its newest run for the Last run column and wires the action buttons to the parent callbacks. */
function TaskRow({
  rule,
  busy,
  onRun,
  onToggle,
  onEdit,
  onHistory,
  onThread,
  onDelete,
}: {
  rule: Automation;
  busy: boolean;
  onRun: () => void;
  onToggle: () => void;
  onEdit: (consentStale: boolean) => void;
  onHistory: () => void;
  onThread: () => void;
  onDelete: () => void;
}) {
  const t = useTranslations('automations');
  const display = useDisplayPreferences();
  const last = useQuery({ queryKey: ['automation-last-run', rule.id, rule.revision], queryFn: () => getLastRun(rule.id) });
  const conversation = useQuery({ queryKey: ['automation-conversation', rule.id], queryFn: () => getRuleConversationId(rule.id) });
  const run = last.data?.items[0];
  const stale = isConsentStale(rule, run);
  const cron = rule.trigger.cron ?? '';
  const label = (key: string, text: string, handler: () => void, icon: React.ReactNode, disabled = false) => (
    <Button key={key} type="button" variant="ghost" size="icon" aria-label={text} title={text} disabled={disabled} onClick={handler}>
      {icon}
    </Button>
  );
  return (
    <TableRow>
      <TableCell>
        <div className="font-medium">{rule.name}</div>
        <TaskSummary rule={rule} run={run} stale={stale} onEdit={() => onEdit(true)} />
      </TableCell>
      <TableCell>{rule.enabled ? t('scheduledTasks.state.active') : t('scheduledTasks.state.paused')}</TableCell>
      <TableCell>
        <div className="font-mono text-xs">{cron}</div>
        <div className="text-xs text-muted-foreground">
          <ScheduleText cron={cron} />
        </div>
      </TableCell>
      <TableCell className="text-sm">
        {run
          ? `${formatDateTime(run.created_at, display.locale, display.timezone)} · ${runStatusLabel(t, run.status)}`
          : t('scheduledTasks.never')}
      </TableCell>
      <TableCell className="text-sm">
        {rule.next_run_at ? formatDateTime(rule.next_run_at, display.locale, display.timezone) : '—'}
      </TableCell>
      <TableCell>
        <div className="flex gap-1">
          {label('run', t('scheduledTasks.runNow'), onRun, <PlayIcon size={16} aria-hidden />, busy || !rule.enabled)}
          {label(
            'toggle',
            rule.enabled ? t('scheduledTasks.pause') : t('scheduledTasks.resume'),
            onToggle,
            rule.enabled ? <PauseIcon size={16} aria-hidden /> : <PlayIcon size={16} aria-hidden />,
            busy,
          )}
          {label('edit', t('scheduledTasks.edit'), () => onEdit(stale), <PencilIcon size={16} aria-hidden />)}
          {label('history', t('scheduledTasks.history'), onHistory, <HistoryIcon size={16} aria-hidden />)}
          {label('thread', t('scheduledTasks.openThread'), onThread, <MessageSquareIcon size={16} aria-hidden />, !conversation.data)}
          <TopicActions rule={rule} label={label} onDelete={onDelete} />
        </div>
      </TableCell>
    </TableRow>
  );
}

/** Empty state of the Yours tab: clock icon, message and the New task button. */
function EmptyYours({ menu }: { menu: React.ReactNode }) {
  const t = useTranslations('automations');
  return (
    <div className="flex flex-col items-center gap-2 py-8 text-center">
      <ClockIcon size={28} aria-hidden className="text-muted-foreground" />
      <h3 className="font-semibold">{t('scheduledTasks.emptyTitle')}</h3>
      <p className="text-sm text-muted-foreground">{t('scheduledTasks.emptyBody')}</p>
      {menu}
    </div>
  );
}

/**
 * Mutations and navigation behind the row actions: run now (idempotent client request id + expected revision),
 * pause/resume (revision-fenced PATCH of `enabled`) and open-thread (resolves the conversation id at click time, because it
 * is null until the first post, then routes to `/chat/{id}`). Failures set a localized notice and reload the list.
 */
function useTaskActions(csrfToken: string, setNotice: (message: string | null) => void) {
  const t = useTranslations('automations');
  const client = useQueryClient();
  const router = useRouter();
  const refresh = () => void client.invalidateQueries({ queryKey: ['automations'] });
  const fail = (error: unknown) => {
    setNotice(t(errorMessageKey(error, 'action.failed')));
    refresh();
  };
  const run = useMutation({
    mutationFn: (rule: Automation) => runAutomation(rule.id, rule.revision, crypto.randomUUID(), csrfToken),
    onSuccess: (_run, rule) => {
      setNotice(t('run.queued'));
      void client.invalidateQueries({ queryKey: ['automation-last-run', rule.id] });
      void client.invalidateQueries({ queryKey: ['automation-conversation', rule.id] });
    },
    onError: fail,
  });
  const toggle = useMutation({
    mutationFn: (rule: Automation) => patchAutomation(rule.id, rule.revision, { enabled: !rule.enabled }, csrfToken),
    onSuccess: () => {
      setNotice(null);
      refresh();
    },
    onError: fail,
  });
  const openThread = async (rule: Automation) => {
    const id = await getRuleConversationId(rule.id);
    if (id) router.push(`/chat/${id}`);
  };
  return { run, toggle, openThread };
}

/** Column headers of the task table. */
function TaskTableHead() {
  const t = useTranslations('automations');
  return (
    <TableHeader>
      <TableRow>
        <TableHead>{t('scheduledTasks.colTask')}</TableHead>
        <TableHead>{t('scheduledTasks.colState')}</TableHead>
        <TableHead>{t('scheduledTasks.colSchedule')}</TableHead>
        <TableHead>{t('scheduledTasks.colLastRun')}</TableHead>
        <TableHead>{t('scheduledTasks.colNextRun')}</TableHead>
        <TableHead>{t('scheduledTasks.colActions')}</TableHead>
      </TableRow>
    </TableHeader>
  );
}

/** Loading and load-failed (with retry) states of the rule list query; renders nothing once data exists. */
function RulesStatus({ rules }: { rules: UseQueryResult<{ items: Automation[] }> }) {
  const t = useTranslations('automations');
  return (
    <>
      {rules.isPending && (
        <p role="status" className="text-sm text-muted-foreground">
          {t('loading')}
        </p>
      )}
      {rules.isError && (
        <div role="alert" className="space-y-2 text-sm">
          <p className="text-destructive">{t('loadFailed')}</p>
          <Button variant="outline" onClick={() => void rules.refetch()}>
            {t('retry')}
          </Button>
        </div>
      )}
    </>
  );
}

/** "New task" dropdown: "From a template", "Blank task" and "Describe in chat" (a link to the chat, where the assistant drafts a task card). */
function NewTaskMenu({ onTemplates, onBlank }: { onTemplates: () => void; onBlank: () => void }) {
  const t = useTranslations('automations');
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button">{t('scheduledTasks.newTask')} ▾</Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={onTemplates}>{t('scheduledTasks.fromTemplate')}</DropdownMenuItem>
        <DropdownMenuItem onSelect={onBlank}>{t('scheduledTasks.blankTask')}</DropdownMenuItem>
        <DropdownMenuItem asChild>
          <Link href="/chat">{t('scheduledTasks.describeInChat')}</Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Applies the chat deep links once the rules load: `?edit={automation id}` opens that task's editor and `?run={run id}`
 * opens the run history of the task named by `?task={automation id}` (the run id only marks the intent), falling back
 * to the daily brief task (one per workspace) when `task` is absent or unknown. Each link is
 * applied once per page load so closing the editor does not reopen it.
 */
function useDeepLink(
  params: ReturnType<typeof useSearchParams>,
  items: Automation[],
  setEditor: (editor: Editor) => void,
  setView: (view: View) => void,
) {
  const [applied, setApplied] = useState(false);
  const editId = params?.get('edit');
  const hasRun = Boolean(params?.get('run'));
  // Adjusting state during render (not in an effect) once the rules first arrive, so the list never flashes first.
  if (applied || items.length === 0) return;
  const edited = editId ? items.find((rule) => rule.id === editId && scheduledTaskKind(rule)) : undefined;
  const taskId = params?.get('task');
  const brief = hasRun
    ? ((taskId ? items.find((rule) => rule.id === taskId && scheduledTaskKind(rule)) : undefined) ??
      items.find((rule) => scheduledTaskKind(rule) === 'daily_brief'))
    : undefined;
  setApplied(true);
  if (edited) setEditor({ mode: 'edit', rule: edited });
  else if (brief) setView({ kind: 'runs', ruleId: brief.id });
}

/**
 * Modal layer of the workspace: the Topic watch editor or the Daily brief editor for the current `editor` state, and
 * the Topic watch delete dialog for `deleting`. Renders nothing while both are null.
 */
function TaskDialogs({
  editor,
  deleting,
  legacy,
  csrfToken,
  onSaved,
  onCloseEditor,
  onCloseDelete,
}: {
  editor: Editor | null;
  deleting: Automation | null;
  legacy: Automation | undefined;
  csrfToken: string;
  onSaved: () => void;
  onCloseEditor: () => void;
  onCloseDelete: () => void;
}) {
  const rule = editor?.mode === 'edit' ? editor.rule : undefined;
  const consentStale = editor?.mode === 'edit' && editor.consentStale === true;
  return (
    <>
      {editor && isTopicEditor(editor) && (
        <TopicWatchEditor rule={rule} consentStale={consentStale} csrfToken={csrfToken} onSaved={onSaved} onClose={onCloseEditor} />
      )}
      {editor && !isTopicEditor(editor) && (
        <BriefTaskEditor
          rule={rule}
          legacyRule={legacy}
          convert={editor.mode === 'convert'}
          csrfToken={csrfToken}
          onSaved={onSaved}
          onClose={onCloseEditor}
        />
      )}
      {deleting && <TopicWatchDeleteDialog rule={deleting} csrfToken={csrfToken} onClose={onCloseDelete} />}
    </>
  );
}

/**
 * Settings › Scheduled tasks workspace: Yours / Templates tabs, the "New task" menu, the task table and the Daily brief
 * editor dialog. Tasks are the rules classified by `scheduledTaskKind`; run, pause/resume, edit, history and open-thread
 * act on those rules through the automations API with revision fences. A successful save returns to the Yours tab. Errors surface as one localized status line.
 */
export function ScheduledTasksWorkspace() {
  const t = useTranslations('automations');
  const { csrfToken } = useWorkspaceSession();
  const [tab, setTab] = useState('yours');
  const [view, setView] = useState<View>({ kind: 'list' });
  const [editor, setEditor] = useState<Editor | null>(null);
  const [deleting, setDeleting] = useState<Automation | null>(null);
  const params = useSearchParams();
  const [notice, setNotice] = useState<string | null>(null);
  const rules = useQuery({ queryKey: ['automations'], queryFn: listAutomations });
  const { run, toggle, openThread } = useTaskActions(csrfToken, setNotice);

  const items = rules.data?.items ?? [];
  useDeepLink(params, items, setEditor, setView);
  const tasks = items.filter((rule) => scheduledTaskKind(rule) !== null);
  const legacy = items.find(isLegacyBrief);
  const selected = view.kind === 'runs' ? items.find((rule) => rule.id === view.ruleId) : undefined;
  const newTaskMenu = <NewTaskMenu onTemplates={() => setTab('templates')} onBlank={() => setView({ kind: 'blank' })} />;
  const templates = (
    <TemplateGrid
      hasTask={tasks.length > 0}
      hasLegacy={legacy !== undefined}
      onUse={(template) => setEditor({ mode: 'new', kind: template.kind })}
      onConvert={() => setEditor({ mode: 'convert' })}
    />
  );

  if (view.kind === 'blank') return <RuleEditor csrfToken={csrfToken} onDone={() => setView({ kind: 'list' })} />;
  if (view.kind === 'runs' && selected) {
    return <RunDetail rule={selected} csrfToken={csrfToken} onBack={() => setView({ kind: 'list' })} />;
  }
  return (
    <section className="space-y-4" aria-label={t('scheduledTasks.title')}>
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="space-y-1">
          <h2 className="text-xl font-semibold">{t('scheduledTasks.title')}</h2>
          <p className="text-sm text-muted-foreground">{t('scheduledTasks.description')}</p>
        </div>
        {tasks.length > 0 && newTaskMenu}
      </header>
      {notice && (
        <p role="status" className="text-sm">
          {notice}
        </p>
      )}
      <RulesStatus rules={rules} />
      {rules.data && (
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="yours">{t('scheduledTasks.tabYours')}</TabsTrigger>
            <TabsTrigger value="templates">{t('scheduledTasks.tabTemplates')}</TabsTrigger>
          </TabsList>
          <TabsContent value="yours" className="space-y-4">
            {tasks.length === 0 ? (
              <>
                <EmptyYours menu={newTaskMenu} />
                <div className="space-y-3 border-t border-dashed border-border pt-4">
                  <h3 className="text-sm font-semibold">{t('scheduledTasks.templatesHeading')}</h3>
                  {templates}
                </div>
              </>
            ) : (
              <Table>
                <TaskTableHead />
                <TableBody>
                  {tasks.map((rule) => (
                    <TaskRow
                      key={rule.id}
                      rule={rule}
                      busy={run.isPending || toggle.isPending}
                      onRun={() => run.mutate(rule)}
                      onToggle={() => toggle.mutate(rule)}
                      onEdit={(consentStale) => setEditor({ mode: 'edit', rule, consentStale })}
                      onHistory={() => setView({ kind: 'runs', ruleId: rule.id })}
                      onThread={() => void openThread(rule)}
                      onDelete={() => setDeleting(rule)}
                    />
                  ))}
                </TableBody>
              </Table>
            )}
          </TabsContent>
          <TabsContent value="templates">{templates}</TabsContent>
        </Tabs>
      )}
      <TaskDialogs
        editor={editor}
        deleting={deleting}
        legacy={legacy}
        csrfToken={csrfToken}
        onSaved={() => setTab('yours')}
        onCloseEditor={() => setEditor(null)}
        onCloseDelete={() => setDeleting(null)}
      />
    </section>
  );
}
