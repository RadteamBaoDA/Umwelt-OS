'use client';

import Link from 'next/link';
import React from 'react';
import { Button } from '@/components/ui/button';
import { cn } from 'cn';

/** Optional follow-up action of a gadget notice: a link (`href`) or a handler (`onClick`). */
export interface GadgetNoticeAction {
  /** Visible action label, e.g. "Configure". */
  label: string;
  /** Internal route to open; wins over `onClick`. */
  href?: string;
  /** Handler used when no `href` is given. */
  onClick?: () => void;
}

/** Props for the unified gadget setup/empty/error notice. */
export interface GadgetNoticeProps {
  /** `setup` and `empty` are neutral; only `error` uses the destructive colour. */
  kind: 'setup' | 'empty' | 'error';
  /** One sentence saying what is missing or what failed. */
  message: string;
  /** Optional single follow-up action. */
  action?: GadgetNoticeAction;
  /** Extra layout classes for the wrapper. */
  className?: string;
}

/**
 * Render the one notice every gadget uses for setup, empty and error states.
 * Errors announce as `role="alert"`; setup and empty states as polite `role="status"`.
 * Pure presentation: callers keep their existing message strings and decide when it shows.
 */
export function GadgetNotice({ kind, message, action, className }: GadgetNoticeProps) {
  const isError = kind === 'error';
  const actionClass = 'inline-flex min-h-11 items-center text-sm text-primary underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring';
  return (
    <div
      role={isError ? 'alert' : 'status'}
      data-gadget-notice={kind}
      className={cn('flex flex-col items-start gap-1 p-3 text-sm', isError ? 'text-destructive' : 'text-muted-foreground', className)}
    >
      <p className="break-words">{message}</p>
      {action?.href ? <Link href={action.href} className={actionClass}>{action.label}</Link>
        : action?.onClick ? <Button type="button" variant="link" className="h-auto min-h-11 p-0" onClick={action.onClick}>{action.label}</Button>
          : null}
    </div>
  );
}

/** Props for the loading skeleton. */
export interface GadgetSkeletonProps {
  /** Shape of the final content: rows, table, single metric or chart. */
  variant: 'list' | 'table' | 'metric' | 'chart';
  /** Original loading sentence, kept as visually hidden status text for assistive tech and tests. */
  label: string;
  /** Extra layout classes for the wrapper. */
  className?: string;
}

/**
 * Render token-coloured placeholder blocks shaped like the final gadget content.
 * The blocks are decorative (`aria-hidden`, pulse only when motion is allowed); the
 * loading message stays available as a visually hidden `role="status"` text.
 */
export function GadgetSkeleton({ variant, label, className }: GadgetSkeletonProps) {
  const block = 'rounded bg-muted motion-safe:animate-pulse';
  return (
    <div role="status" className={cn('p-3', className)}>
      <span className="sr-only">{label}</span>
      <div aria-hidden="true" className="space-y-2">
        {variant === 'metric' ? <>
          <div className={cn(block, 'h-8 w-1/3')} />
          <div className={cn(block, 'h-3 w-2/3')} />
        </> : variant === 'chart' ? <div className={cn(block, 'h-32 w-full')} />
          : variant === 'table' ? <>
            <div className={cn(block, 'h-3 w-full')} />
            {[0, 1, 2, 3].map((row) => <div key={row} className={cn(block, 'h-5 w-full opacity-70')} />)}
          </> : [0, 1, 2].map((row) => <div key={row} className="space-y-1">
            <div className={cn(block, 'h-4 w-3/4')} />
            <div className={cn(block, 'h-3 w-1/2 opacity-70')} />
          </div>)}
      </div>
    </div>
  );
}
