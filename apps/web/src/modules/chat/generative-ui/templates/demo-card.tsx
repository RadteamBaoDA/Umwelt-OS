'use client';

import * as React from 'react';
import { ArrowUpRightIcon } from 'lucide-react';
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
import { genUiCopy, resolveWidgetLocale } from '../copy';
import { getDemoCategoryLabel, type DemoItem } from './demo-data';

/** Props accepted by the DemoCard gallery component. */
export interface DemoCardProps {
  /** Demo item metadata and localized prompts. */
  demo: DemoItem;
  /** Application locale ('en-us' | 'vi-vi'). */
  locale?: AppLocaleId;
  /** Callback invoked with the localized prompt when the user clicks the demo card. */
  onSelectPrompt: (prompt: string, demo: DemoItem) => void;
}

/**
 * Interactive DemoCard displaying a Generative UI capability example in the starter gallery.
 *
 * @param props - DemoCardProps configuration and selection callback.
 * @returns Accessible demo card element.
 */
export function DemoCard({
  demo,
  locale,
  onSelectPrompt,
}: DemoCardProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const copy = { tryLabel: genUiCopy(resolvedLocale).genUiDemoTry };
  const title = demo.title[resolvedLocale];
  const description = demo.description[resolvedLocale];
  const prompt = demo.prompt[resolvedLocale];
  const categoryLabel = getDemoCategoryLabel(demo.category, resolvedLocale);

  return (
    <Card
      data-testid={`demo-card-${demo.id}`}
      className="flex flex-col justify-between gap-3 border-border bg-surface py-4 text-foreground shadow-sm transition-colors hover:border-primary/50"
    >
      <CardHeader className="gap-1.5 px-4 pb-0">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm font-semibold text-foreground">{title}</CardTitle>
          <Badge variant="outline" className="text-[11px]">
            {categoryLabel}
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="px-4 py-0">
        <CardDescription className="text-xs leading-relaxed text-muted-foreground">
          {description}
        </CardDescription>
      </CardContent>

      <CardFooter className="px-4 pt-1">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={() => onSelectPrompt(prompt, demo)}
          data-testid={`demo-card-action-${demo.id}`}
          className="w-full justify-between text-xs"
        >
          <span>{copy.tryLabel}</span>
          <ArrowUpRightIcon className="size-3.5" aria-hidden="true" />
        </Button>
      </CardFooter>
    </Card>
  );
}

