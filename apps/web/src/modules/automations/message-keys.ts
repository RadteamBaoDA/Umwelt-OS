import type { useTranslations } from 'next-intl';

/** Translator bound to the `automations` namespace. */
export type AutomationT = ReturnType<typeof useTranslations<'automations'>>;

/**
 * Literal catalog keys for server-provided enum values. Tables replace runtime key building (`trigger_${type}`) so
 * every key is checked against locales/en-us/automations.json at compile time.
 */
const TRIGGER_KEYS = {
  schedule: 'trigger.schedule',
  new_event: 'trigger.newEvent',
  new_document: 'trigger.newDocument',
  entity_changed: 'trigger.entityChanged',
  task_due: 'trigger.taskDue',
  goal_deadline: 'trigger.goalDeadline',
  webhook: 'trigger.webhook',
  connector_sync_result: 'trigger.connectorSyncResult',
  manual: 'trigger.manual',
} as const;
const ACTION_KEYS = {
  run_agent: 'action.runAgent',
  create_task: 'action.createTask',
  create_notification: 'action.createNotification',
  generate_brief: 'action.generateBrief',
  call_webhook: 'action.callWebhook',
  collect_source: 'action.collectSource',
} as const;
const OPERATOR_KEYS = {
  eq: 'op.eq',
  ne: 'op.ne',
  in: 'op.in',
  gt: 'op.gt',
  gte: 'op.gte',
  lt: 'op.lt',
  lte: 'op.lte',
} as const;
const OUTCOME_KEYS = {
  matched: 'outcome.matched',
  not_matched: 'outcome.notMatched',
  missing_field: 'outcome.missingField',
} as const;
const RUN_STATUS_KEYS = {
  queued: 'run.statusQueued',
  running: 'run.statusRunning',
  awaiting_approval: 'run.statusAwaitingApproval',
  succeeded: 'run.statusSucceeded',
  failed: 'run.statusFailed',
  skipped: 'run.statusSkipped',
  dropped: 'run.statusDropped',
  requires_review: 'run.statusRequiresReview',
} as const;
const ACTION_STATUS_KEYS = {
  pending: 'action.statusPending',
  awaiting_approval: 'action.statusAwaitingApproval',
  approved: 'action.statusApproved',
  in_flight: 'action.statusInFlight',
  succeeded: 'action.statusSucceeded',
  failed: 'action.statusFailed',
  denied: 'action.statusDenied',
  skipped: 'action.statusSkipped',
  requires_review: 'action.statusRequiresReview',
} as const;

/** Localized trigger name for a server trigger type; unknown types fall back to the raw value. */
export function triggerLabel(t: AutomationT, type: string): string {
  return Object.hasOwn(TRIGGER_KEYS, type) ? t(TRIGGER_KEYS[type as keyof typeof TRIGGER_KEYS]) : type;
}

/** Localized action name for a server action type; unknown types fall back to the raw value. */
export function actionLabel(t: AutomationT, type: string): string {
  return Object.hasOwn(ACTION_KEYS, type) ? t(ACTION_KEYS[type as keyof typeof ACTION_KEYS]) : type;
}

/** Localized condition operator; unknown operators fall back to the raw value. */
export function operatorLabel(t: AutomationT, op: string): string {
  return Object.hasOwn(OPERATOR_KEYS, op) ? t(OPERATOR_KEYS[op as keyof typeof OPERATOR_KEYS]) : op;
}

/** Localized dry-run outcome; unknown outcomes fall back to the raw value. */
export function outcomeLabel(t: AutomationT, outcome: string): string {
  return Object.hasOwn(OUTCOME_KEYS, outcome) ? t(OUTCOME_KEYS[outcome as keyof typeof OUTCOME_KEYS]) : outcome;
}

/** Localized run status; unknown statuses fall back to the raw value. */
export function runStatusLabel(t: AutomationT, status: string): string {
  return Object.hasOwn(RUN_STATUS_KEYS, status) ? t(RUN_STATUS_KEYS[status as keyof typeof RUN_STATUS_KEYS]) : status;
}

/** Localized per-action status; unknown statuses fall back to the raw value. */
export function actionStatusLabel(t: AutomationT, status: string): string {
  return Object.hasOwn(ACTION_STATUS_KEYS, status) ? t(ACTION_STATUS_KEYS[status as keyof typeof ACTION_STATUS_KEYS]) : status;
}

/** Message keys produced by rule-form validation (`toDefinition`). */
export type AutomationFormErrorKey = 'err.cron' | 'err.webhookAlias' | 'err.condition' | 'err.noActions' | 'err.action' | 'err.name';

/** Message keys returned by `errorMessageKey` for a failed automation request. */
export type AutomationRequestErrorKey =
  | 'conflict.label'
  | 'conflict.briefSlot'
  | 'conflict.quota'
  | 'conflict.generic'
  | 'pauseBeforeBriefEdit'
  | 'invalidServer'
  | 'action.failed'
  | 'saveFailed'
  | 'scheduledTasks.topic.consentMismatch';
