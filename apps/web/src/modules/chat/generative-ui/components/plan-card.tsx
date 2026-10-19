'use client';

import * as React from 'react';
import { CheckCircle2Icon, CircleDotIcon, ClockIcon, ListChecksIcon, SparklesIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import type { AppLocaleId } from '@/core/i18n';
import { genUiCopy, PLAN_COPY, resolveWidgetLocale } from '../copy';

/** Execution lifecycle status for an individual plan step. */
export type PlanStepStatus = 'pending' | 'in_progress' | 'completed';

/** Structured step descriptor within an interactive execution plan. */
export interface PlanStep {
  /** Stable identifier for the step within the plan. */
  id: string;
  /** Concise action title displayed for the step (canonical frontend key). */
  label?: string;
  /** Backend PlanStep title alias (`modules/chat/generative_ui/tools/plan.py`). */
  title?: string;
  /** Upstream OpenIntelligentUI Todo text alias. */
  text?: string;
  /** Optional emoji prefix from backend/upstream plan step schema. */
  emoji?: string;
  /** Optional rationale or detail text explaining the step. */
  description?: string;
  /** Current status of the step. */
  status?: PlanStepStatus;
  /** Upstream boolean completion flag when `status` is omitted. */
  completed?: boolean;
  /** Optional workspace citation provenance identifiers. */
  source_id?: string | null;
  document_id?: string | null;
  chunk_id?: string | null;
}

/** Normalized internal step with guaranteed `label` and `status`. */
interface NormalizedPlanStep extends PlanStep {
  label: string;
  status: PlanStepStatus;
}

/** Props accepted by the native structured PlanCard widget. */
export interface PlanCardProps {
  /** Headline title of the execution plan. */
  title: string;
  /** Optional summary description or objective for the plan. */
  description?: string;
  /** Ordered list of interactive plan steps. */
  steps?: PlanStep[];
  /** Optional upstream/backend `todos` alias when `steps` is omitted. */
  todos?: PlanStep[];
  /** Optional architectural approach summary from upstream planning metadata. */
  approach?: string;
  /** Optional technology stack label from upstream planning metadata. */
  technology?: string;
  /** Optional key element tags highlighted in the plan header (camelCase). */
  keyElements?: string[];
  /** Optional snake_case alias emitted by Python `PlanPayload.key_elements`. */
  key_elements?: string[];
  /** Optional provenance source ('workspace_knowledge' | 'sample'). */
  source?: string;
  /** Application locale identifier ('en-us' | 'vi-vi') with automatic fallback. */
  locale?: AppLocaleId;
  /** Callback invoked when the user approves or refines the plan to send a follow-up prompt. */
  onSendPrompt?: (prompt: string) => void;
  /** Optional callback notified whenever an individual step is toggled. */
  onStepToggle?: (stepId: string, nextStatus: PlanStepStatus) => void;
}

export { resolveWidgetLocale };

/**
 * Maps a plan step status to a semantic Badge variant and status label.
 *
 * @param status - Current step execution status.
 * @param locale - Active application locale.
 * @returns Badge variant and localized status string.
 */
function getStepBadgeMeta(
  status: PlanStepStatus,
  locale: AppLocaleId,
): { variant: 'default' | 'secondary' | 'outline'; label: string } {
  const copy = PLAN_COPY[locale];
  if (status === 'completed') {
    return { variant: 'default', label: copy.statusCompleted };
  }
  if (status === 'in_progress') {
    return { variant: 'secondary', label: copy.statusInProgress };
  }
  return { variant: 'outline', label: copy.statusPending };
}

/**
 * Normalizes backend (`title`, `emoji`, `todos`) and frontend (`label`, `steps`) plan step items.
 *
 * @param steps - Primary steps list.
 * @param todos - Optional fallback todos list.
 * @param overrides - User-interactive status overrides keyed by step ID.
 * @returns Normalized plan steps with guaranteed `label` and `status`.
 */
function normalizePlanSteps(
  steps: PlanStep[] | undefined,
  todos: PlanStep[] | undefined,
  overrides: Record<string, PlanStepStatus>,
): NormalizedPlanStep[] {
  const rawList = steps && steps.length > 0 ? steps : (todos ?? []);
  return rawList.map((step, index) => {
    const id = step.id || `step-${index + 1}`;
    const label = step.label || step.title || step.text || `Step ${index + 1}`;
    const baseStatus: PlanStepStatus =
      step.status ?? (step.completed ? 'completed' : 'pending');
    return {
      ...step,
      id,
      label,
      status: overrides[id] ?? baseStatus,
    };
  });
}

/**
 * Native structured PlanCard widget ported from OpenIntelligentUI and adapted to Umwelt-OS
 * shadcn primitives and semantic tokens. Supports interactive step toggling, progress tracking,
 * and follow-up prompt dispatch for approving or refining the plan.
 *
 * @param props - PlanCardProps configuration and callbacks.
 * @returns Accessible interactive execution plan card.
 */
export function PlanCard({
  title,
  description,
  steps,
  todos,
  approach,
  technology,
  keyElements,
  key_elements,
  locale,
  onSendPrompt,
  onStepToggle,
}: PlanCardProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const copy = PLAN_COPY[resolvedLocale];
  const catalog = genUiCopy(resolvedLocale);
  const resolvedKeyElements = keyElements ?? key_elements;

  const [statusOverrides, setStatusOverrides] = React.useState<Record<string, PlanStepStatus>>({});
  const [isApproved, setIsApproved] = React.useState(false);

  const stepItems = React.useMemo(
    () => normalizePlanSteps(steps, todos, statusOverrides),
    [steps, todos, statusOverrides],
  );

  const totalCount = stepItems.length;
  const completedCount = React.useMemo(
    () => stepItems.filter((step) => step.status === 'completed').length,
    [stepItems],
  );
  const progressPercent = totalCount > 0 ? Math.round((completedCount / totalCount) * 100) : 0;

  /**
   * Toggles a single step between 'completed' and 'pending' when interacted with by the user.
   */
  const handleToggleStep = React.useCallback(
    (stepId: string) => {
      const target = stepItems.find((step) => step.id === stepId);
      if (!target) return;
      const nextStatus: PlanStepStatus =
        target.status === 'completed' ? 'pending' : 'completed';
      onStepToggle?.(stepId, nextStatus);
      setStatusOverrides((prev) => ({ ...prev, [stepId]: nextStatus }));
    },
    [onStepToggle, stepItems],
  );

  /**
   * Approves the current plan state and dispatches a structured execution prompt to the chat session.
   */
  const handleApprove = React.useCallback(() => {
    setIsApproved(true);
    const activeLabels = stepItems.map((step) => step.label);
    onSendPrompt?.(copy.approvePrompt(title, activeLabels));
  }, [copy, onSendPrompt, stepItems, title]);

  /**
   * Requests plan refinement by sending a follow-up prompt to the assistant.
   */
  const handleRefine = React.useCallback(() => {
    onSendPrompt?.(copy.refinePrompt(title));
  }, [copy, onSendPrompt, title]);

  return (
    <Card
      data-testid="generative-plan-card"
      className="my-2 w-full max-w-2xl border-border bg-surface text-foreground shadow-sm"
    >
      <CardHeader className="gap-2 border-b border-border pb-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <ListChecksIcon className="size-4" aria-hidden="true" />
            </span>
            <CardTitle className="text-base font-semibold text-foreground">{title}</CardTitle>
          </div>
          <Badge
            variant="secondary"
            data-testid="plan-progress-counter"
            className="font-mono text-xs"
          >
            {copy.progressLabel(completedCount, totalCount)}
          </Badge>
        </div>

        {description && (
          <CardDescription className="text-xs leading-relaxed text-muted-foreground">
            {description}
          </CardDescription>
        )}

        {(approach || technology || (resolvedKeyElements && resolvedKeyElements.length > 0)) && (
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {approach && (
              <span>
                <strong className="font-medium text-foreground">{copy.approachLabel}:</strong>{' '}
                {approach}
              </span>
            )}
            {technology && (
              <span>
                <strong className="font-medium text-foreground">{copy.technologyLabel}:</strong>{' '}
                {technology}
              </span>
            )}
            {resolvedKeyElements?.map((element) => (
              <Badge key={element} variant="outline" className="text-[11px]">
                {element}
              </Badge>
            ))}
          </div>
        )}

        {/* Semantic progress bar */}
        <div
          role="progressbar"
          aria-valuenow={completedCount}
          aria-valuemin={0}
          aria-valuemax={totalCount}
          aria-label={copy.progressLabel(completedCount, totalCount)}
          className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-muted"
        >
          <div
            className="h-full bg-primary transition-all duration-200"
            style={{ width: `${progressPercent}%` }}
          />
        </div>
      </CardHeader>

      <CardContent className="pt-4">
        <ol className="space-y-2.5" aria-label={title}>
          {stepItems.map((step) => {
            const isCompleted = step.status === 'completed';
            const isInProgress = step.status === 'in_progress';
            const badgeMeta = getStepBadgeMeta(step.status, resolvedLocale);

            return (
              <li
                key={step.id}
                data-testid={`plan-step-${step.id}`}
                data-status={step.status}
                className={`flex items-start justify-between gap-3 rounded-lg border p-3 transition-colors ${
                  isCompleted
                    ? 'border-border bg-muted/40'
                    : isInProgress
                      ? 'border-primary/40 bg-primary/5'
                      : 'border-border bg-background'
                }`}
              >
                <div className="flex min-w-0 flex-1 items-start gap-3">
                  <Checkbox
                    id={`plan-step-checkbox-${step.id}`}
                    checked={isCompleted}
                    onCheckedChange={() => handleToggleStep(step.id)}
                    aria-label={step.label}
                    className="mt-0.5"
                  />
                  <div className="min-w-0 flex-1">
                    <label
                      htmlFor={`plan-step-checkbox-${step.id}`}
                      className={`block cursor-pointer text-sm font-medium leading-snug ${
                        isCompleted ? 'line-through text-muted-foreground' : 'text-foreground'
                      }`}
                    >
                      {step.emoji && (
                        <span className="mr-1.5" aria-hidden="true">
                          {step.emoji}
                        </span>
                      )}
                      {step.label}
                    </label>
                    {step.description && (
                      <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                        {step.description}
                      </p>
                    )}
                  </div>
                </div>

                <div className="flex shrink-0 items-center gap-1.5">
                  {isCompleted ? (
                    <CheckCircle2Icon className="size-4 text-primary" aria-hidden="true" />
                  ) : isInProgress ? (
                    <CircleDotIcon className="size-4 text-primary animate-pulse" aria-hidden="true" />
                  ) : (
                    <ClockIcon className="size-4 text-muted-foreground" aria-hidden="true" />
                  )}
                  <Badge variant={badgeMeta.variant} className="text-[11px]">
                    {badgeMeta.label}
                  </Badge>
                </div>
              </li>
            );
          })}
        </ol>

        {isApproved && (
          <p
            role="status"
            data-testid="plan-approved-banner"
            className="mt-3 flex items-center gap-2 rounded-lg border border-primary/30 bg-primary/10 px-3 py-2 text-xs font-medium text-primary"
          >
            <SparklesIcon className="size-4 shrink-0" aria-hidden="true" />
            <span>{copy.approvedNotice}</span>
          </p>
        )}
      </CardContent>

      <CardFooter className="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-4">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={handleRefine}
          data-testid="plan-refine-button"
        >
          {catalog.genUiPlanRefine}
        </Button>
        <Button
          type="button"
          variant="default"
          size="sm"
          onClick={handleApprove}
          data-testid="plan-approve-button"
        >
          {catalog.genUiPlanApprove}
        </Button>
      </CardFooter>
    </Card>
  );
}

