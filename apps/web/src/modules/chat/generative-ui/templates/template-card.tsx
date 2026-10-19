'use client';

import * as React from 'react';
import {
  ArrowUpRightIcon,
  BarChart3Icon,
  CalculatorIcon,
  GitBranchIcon,
  ListChecksIcon,
  TableIcon,
} from 'lucide-react';
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
import type { AppLocaleId } from '@/core/i18n';
import { resolveWidgetLocale } from '../components/plan-card';
import {
  getLocalizedTemplate,
  type SeedTemplate,
  type TemplateCategory,
} from './seed-templates';

/** Props accepted by the interactive TemplateCard component. */
export interface TemplateCardProps {
  /** Starter template definition. */
  template: SeedTemplate;
  /** Application locale ('en-us' | 'vi-vi'). */
  locale?: AppLocaleId;
  /** Callback invoked with the localized prompt when the user selects the template. */
  onSelectPrompt: (prompt: string, template: SeedTemplate) => void;
}

/** Bilingual button labels for TemplateCard. */
const TEMPLATE_CARD_COPY: Record<AppLocaleId, { tryPrompt: string }> = {
  'en-us': { tryPrompt: 'Use template' },
  'vi-vi': { tryPrompt: 'Dùng mẫu này' },
};

/**
 * Renders the appropriate semantic icon for a starter template category.
 *
 * @param category - Starter template category.
 * @returns Lucide icon element.
 */
function renderCategoryIcon(category: TemplateCategory) {
  switch (category) {
    case 'diagrams':
      return <GitBranchIcon className="size-4" aria-hidden="true" />;
    case 'calculators':
      return <CalculatorIcon className="size-4" aria-hidden="true" />;
    case 'charts':
      return <BarChart3Icon className="size-4" aria-hidden="true" />;
    case 'tables':
      return <TableIcon className="size-4" aria-hidden="true" />;
    case 'planning':
      return <ListChecksIcon className="size-4" aria-hidden="true" />;
  }
}

/**
 * Interactive starter card for the Generative UI template library.
 * Displays localized title, category badge, description, and dispatches the starter prompt on click.
 *
 * @param props - TemplateCardProps configuration and callback.
 * @returns Accessible starter template card.
 */
export function TemplateCard({
  template,
  locale,
  onSelectPrompt,
}: TemplateCardProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const localized = getLocalizedTemplate(template, resolvedLocale);
  const copy = TEMPLATE_CARD_COPY[resolvedLocale];

  return (
    <Card
      data-testid={`template-card-${template.id}`}
      className="flex flex-col justify-between gap-3 border-border bg-surface py-4 text-foreground shadow-sm transition-colors hover:border-primary/50"
    >
      <CardHeader className="gap-2 px-4 pb-0">
        <div className="flex items-center justify-between gap-2">
          <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
            {renderCategoryIcon(template.category)}
          </span>
          <Badge variant="secondary" className="text-[11px]">
            {localized.categoryLabel}
          </Badge>
        </div>
        <CardTitle className="text-sm font-semibold text-foreground">
          {localized.title}
        </CardTitle>
      </CardHeader>

      <CardContent className="px-4 py-0">
        <CardDescription className="text-xs leading-relaxed text-muted-foreground">
          {localized.description}
        </CardDescription>
      </CardContent>

      <CardFooter className="px-4 pt-1">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onSelectPrompt(localized.prompt, template)}
          data-testid={`template-card-action-${template.id}`}
          className="w-full justify-between text-xs"
        >
          <span>{copy.tryPrompt}</span>
          <ArrowUpRightIcon className="size-3.5" aria-hidden="true" />
        </Button>
      </CardFooter>
    </Card>
  );
}

