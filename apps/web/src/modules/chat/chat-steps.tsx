'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { CheckIcon, ChevronDownIcon, ChevronRightIcon, Loader2Icon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import type { ToolStep } from './api';

type Translator = ReturnType<typeof useTranslations<'chat'>>;

/** Message keys (running, finished) of the tools whose copy needs no placeholder. */
const STEP_KEYS: Record<
  string,
  readonly ['stepRetrievalRunning' | 'stepWebRunning' | 'stepDataRunning', 'stepRetrievalDoneNoCount' | 'stepWebDone' | 'stepDataDone']
> = {
  retrieval: ['stepRetrievalRunning', 'stepRetrievalDoneNoCount'],
  web_search: ['stepWebRunning', 'stepWebDone'],
  query_collected_data: ['stepDataRunning', 'stepDataDone'],
};

/**
 * Localized text of one step. Known tools (retrieval, web search, collected data, skills) get specific copy; unknown
 * tools fall back to generic copy that names the optional `label` instead of the raw tool id.
 *
 * @param t - `chat` translator.
 * @param step - Step to describe; `detail.documents` is the retrieval hit count.
 * @returns The running ("Searching…") or finished ("Found 3 documents") sentence.
 */
export function stepText(t: Translator, step: ToolStep): string {
  const running = step.state === 'running';
  const { tool, label, detail } = step;
  if (tool === 'retrieval' && !running) {
    return typeof detail?.documents === 'number' ? t('stepRetrievalDone', { count: detail.documents }) : t('stepRetrievalDoneNoCount');
  }
  // Tools whose copy has no placeholder: running key / finished key.
  const plain = STEP_KEYS[tool];
  if (plain) return t(running ? plain[0] : plain[1]);
  // Skills and unknown tools take the optional label as a subject.
  const family = tool === 'read_skill' ? 'stepSkill' : 'stepGeneric';
  const state = running ? 'Running' : 'Done';
  if (label) return t(`${family}${state}${family === 'stepSkill' ? '' : 'Label'}` as 'stepSkillRunning', { label });
  return t(`${family}${state}${family === 'stepSkill' ? 'NoLabel' : ''}` as 'stepGenericRunning');
}

/**
 * Whether a step is worth showing: a finished retrieval that found no documents is noise for a general-knowledge answer
 * (Q6), so it is hidden; every other step stays.
 *
 * @param step - Step to test.
 */
export function isVisibleStep(step: ToolStep): boolean {
  return !(step.tool === 'retrieval' && step.state === 'done' && step.detail?.documents === 0);
}

/**
 * Compact processing-step list of a run. While streaming it is always expanded (running steps spin, finished ones show a
 * check). Once the answer settled it collapses to a one-line "N steps" toggle that expands on demand.
 *
 * @param props - `steps` in arrival order and whether the run is still streaming.
 * @returns The list, or null when there are no steps.
 */
export function ChatStepList({ steps: allSteps, streaming }: { steps: ToolStep[]; streaming: boolean }) {
  const steps = allSteps.filter(isVisibleStep);
  const t = useTranslations('chat');
  const [open, setOpen] = React.useState(false);
  const listId = React.useId();
  if (steps.length === 0) return null;
  const expanded = streaming || open;
  return (
    <div
      data-testid="step-list"
      role={streaming ? 'status' : undefined}
      aria-live={streaming ? 'polite' : undefined}
      className="flex flex-col gap-1 text-xs text-muted-foreground"
    >
      {!streaming && (
        <Button
          type="button"
          variant="ghost"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen((value) => !value)}
          className="min-h-8 pointer-coarse:min-h-11 w-fit gap-1 px-2 text-xs font-normal text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {open ? (
            <ChevronDownIcon className="size-3.5" aria-hidden="true" />
          ) : (
            <ChevronRightIcon className="size-3.5" aria-hidden="true" />
          )}
          <span>{t('stepsSummary', { count: steps.length })}</span>
          <span className="sr-only">{open ? t('stepsHide') : t('stepsShow')}</span>
        </Button>
      )}
      {expanded && (
        <ul id={listId} aria-label={t('stepsLabel')} className="flex flex-col gap-1 pl-1">
          {steps.map((step, index) => (
            <li key={`${step.tool}:${index}`} data-state={step.state} className="flex items-center gap-2">
              {step.state === 'running' ? (
                <Loader2Icon className="size-3.5 animate-spin" aria-hidden="true" />
              ) : (
                <CheckIcon className="size-3.5" aria-hidden="true" />
              )}
              <span>{stepText(t, step)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
