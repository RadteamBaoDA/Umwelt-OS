'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { CheckIcon, ChevronDownIcon, ChevronRightIcon, Loader2Icon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import type { ToolStep } from './api';

type Translator = ReturnType<typeof useTranslations<'chat'>>;

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
  switch (step.tool) {
    case 'retrieval':
      if (running) return t('stepRetrievalRunning');
      return typeof step.detail?.documents === 'number' ? t('stepRetrievalDone', { count: step.detail.documents }) : t('stepRetrievalDoneNoCount');
    case 'web_search':
      return t(running ? 'stepWebRunning' : 'stepWebDone');
    case 'query_collected_data':
      return t(running ? 'stepDataRunning' : 'stepDataDone');
    case 'read_skill':
      if (step.label) return t(running ? 'stepSkillRunning' : 'stepSkillDone', { label: step.label });
      return t(running ? 'stepSkillRunningNoLabel' : 'stepSkillDoneNoLabel');
    default:
      if (step.label) return t(running ? 'stepGenericRunningLabel' : 'stepGenericDoneLabel', { label: step.label });
      return t(running ? 'stepGenericRunning' : 'stepGenericDone');
  }
}

/**
 * Compact processing-step list of a run. While streaming it is always expanded (running steps spin, finished ones show a
 * check). Once the answer settled it collapses to a one-line "N steps" toggle that expands on demand.
 *
 * @param props - `steps` in arrival order and whether the run is still streaming.
 * @returns The list, or null when there are no steps.
 */
export function ChatStepList({ steps, streaming }: { steps: ToolStep[]; streaming: boolean }) {
  const t = useTranslations('chat');
  const [open, setOpen] = React.useState(false);
  const listId = React.useId();
  if (steps.length === 0) return null;
  const expanded = streaming || open;
  return (
    <div data-testid="step-list" role={streaming ? 'status' : undefined} aria-live={streaming ? 'polite' : undefined} className="flex flex-col gap-1 text-xs text-muted-foreground">
      {!streaming && (
        <Button
          type="button"
          variant="ghost"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen((value) => !value)}
          className="min-h-8 w-fit gap-1 px-2 text-xs font-normal text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {open ? <ChevronDownIcon className="size-3.5" aria-hidden="true" /> : <ChevronRightIcon className="size-3.5" aria-hidden="true" />}
          <span>{t('stepsSummary', { count: steps.length })}</span>
          <span className="sr-only">{open ? t('stepsHide') : t('stepsShow')}</span>
        </Button>
      )}
      {expanded && (
        <ul id={listId} aria-label={t('stepsLabel')} className="flex flex-col gap-1 pl-1">
          {steps.map((step, index) => (
            <li key={`${step.tool}:${index}`} data-state={step.state} className="flex items-center gap-2">
              {step.state === 'running'
                ? <Loader2Icon className="size-3.5 animate-spin" aria-hidden="true" />
                : <CheckIcon className="size-3.5" aria-hidden="true" />}
              <span>{stepText(t, step)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
