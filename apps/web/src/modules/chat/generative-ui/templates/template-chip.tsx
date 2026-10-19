'use client';

import * as React from 'react';
import { SparklesIcon, XIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import type { AppLocaleId } from '@/core/i18n';
import { resolveWidgetLocale } from '../components/plan-card';
import { getLocalizedTemplate, type SeedTemplate } from './seed-templates';

/** Props accepted by the quick-action TemplateChip component. */
export interface TemplateChipProps {
  /** Starter template or custom label/prompt descriptor. */
  template?: SeedTemplate;
  /** Optional custom chip label when `template` is omitted. */
  label?: string;
  /** Optional custom prompt string when `template` is omitted. */
  prompt?: string;
  /** Optional category badge text. */
  categoryLabel?: string;
  /** Application locale ('en-us' | 'vi-vi'). */
  locale?: AppLocaleId;
  /** Callback invoked with the prompt string when the chip is clicked. */
  onSelectPrompt: (prompt: string) => void;
  /** Optional callback to dismiss/clear an active template chip. */
  onClear?: () => void;
}

/** Bilingual labels for TemplateChip dismissal. */
const CHIP_COPY: Record<AppLocaleId, { clearLabel: string }> = {
  'en-us': { clearLabel: 'Remove active template' },
  'vi-vi': { clearLabel: 'Bỏ mẫu đang chọn' },
};

/**
 * Compact quick-action starter chip that dispatches a Generative UI prompt when clicked,
 * with optional clear button when displaying an active template in the composer area.
 *
 * @param props - TemplateChipProps configuration and callbacks.
 * @returns Accessible quick-action template chip.
 */
export function TemplateChip({
  template,
  label,
  prompt,
  categoryLabel,
  locale,
  onSelectPrompt,
  onClear,
}: TemplateChipProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const copy = CHIP_COPY[resolvedLocale];
  const localized = template ? getLocalizedTemplate(template, resolvedLocale) : null;

  const displayLabel = label ?? localized?.title ?? '';
  const targetPrompt = prompt ?? localized?.prompt ?? '';
  const displayCategory = categoryLabel ?? localized?.categoryLabel;

  return (
    <div
      data-testid={template ? `template-chip-${template.id}` : 'template-chip'}
      className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-1 py-0.5 text-xs text-foreground shadow-xs"
    >
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => onSelectPrompt(targetPrompt)}
        className="min-h-8 rounded-full px-2.5 py-1 text-xs font-medium text-foreground hover:bg-secondary"
      >
        <SparklesIcon className="size-3.5 text-primary" aria-hidden="true" />
        <span>{displayLabel}</span>
        {displayCategory && (
          <Badge variant="secondary" className="ml-1 text-[10px]">
            {displayCategory}
          </Badge>
        )}
      </Button>

      {onClear && (
        <button
          type="button"
          onClick={onClear}
          aria-label={copy.clearLabel}
          data-testid="template-chip-clear"
          className="inline-flex size-6 items-center justify-center rounded-full text-muted-foreground hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <XIcon className="size-3" aria-hidden="true" />
        </button>
      )}
    </div>
  );
}

