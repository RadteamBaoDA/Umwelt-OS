'use client';

import * as React from 'react';
import { ChevronRightIcon, WrenchIcon } from 'lucide-react';
import type { AppLocaleId } from '@/core/i18n';
import { fmt, genUiCopy, resolveWidgetLocale } from '../copy';

/** Props for {@link ToolReasoning}. */
export interface ToolReasoningProps {
  /** Tool or payload kind name shown in the summary. */
  name: string;
  /** Tool arguments/payload shown (as JSON text) when expanded. */
  data: unknown;
  /** Application locale. */
  locale?: AppLocaleId;
}

/**
 * Default collapsible renderer for a tool call with no dedicated widget. Collapsed by default;
 * uses a native details element so keyboard and screen-reader toggling come for free.
 *
 * @param props - Tool name, payload and locale.
 * @returns Collapsible tool-call summary.
 */
export function ToolReasoning({ name, data, locale }: ToolReasoningProps) {
  const copy = genUiCopy(resolveWidgetLocale(locale));
  let body: string;
  try {
    body = JSON.stringify(data, null, 2) ?? '';
  } catch {
    body = String(data);
  }
  return (
    <details data-testid="tool-reasoning" className="group rounded-lg border border-border bg-surface text-xs text-muted-foreground">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <ChevronRightIcon className="size-3.5 transition-transform group-open:rotate-90" aria-hidden="true" />
        <WrenchIcon className="size-3.5" aria-hidden="true" />
        <span>{fmt(copy.shellToolReasoning, { name })}</span>
      </summary>
      <pre className="max-h-64 overflow-auto border-t border-border p-3 text-foreground">{body}</pre>
    </details>
  );
}
