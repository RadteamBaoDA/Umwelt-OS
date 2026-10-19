'use client';

import * as React from 'react';
import { Button } from '@/components/ui/button';
import type { AppLocaleId } from '@/core/i18n';
import { resolveWidgetLocale } from '../components/plan-card';
import {
  DEMO_CATEGORIES,
  getDemoCategoryLabel,
  type DemoCategory,
} from './demo-data';

/** Props accepted by the CategoryFilter toolbar component. */
export interface CategoryFilterProps {
  /** Currently active category filter, or null for all categories. */
  selected: DemoCategory | null;
  /** Callback invoked when the user selects a category pill. */
  onSelect: (category: DemoCategory | null) => void;
  /** Application locale ('en-us' | 'vi-vi'). */
  locale?: AppLocaleId;
}

/** Bilingual aria-label for the category filter toolbar. */
const FILTER_COPY: Record<AppLocaleId, { groupLabel: string }> = {
  'en-us': { groupLabel: 'Filter templates by category' },
  'vi-vi': { groupLabel: 'Lọc mẫu theo danh mục' },
};

/**
 * Filterable category pill bar for the Generative UI starter gallery.
 * Uses shadcn Button primitives and semantic tokens.
 *
 * @param props - CategoryFilterProps state and selection callback.
 * @returns Accessible category filter toolbar.
 */
export function CategoryFilter({
  selected,
  onSelect,
  locale,
}: CategoryFilterProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const copy = FILTER_COPY[resolvedLocale];
  const categories: Array<DemoCategory | null> = [null, ...DEMO_CATEGORIES];

  return (
    <div
      role="group"
      aria-label={copy.groupLabel}
      data-testid="generative-category-filter"
      className="flex flex-wrap items-center gap-1.5"
    >
      {categories.map((cat) => {
        const key = cat ?? 'all';
        const isActive = cat === selected;
        const label = getDemoCategoryLabel(cat, resolvedLocale);

        return (
          <Button
            key={key}
            type="button"
            variant={isActive ? 'default' : 'outline'}
            size="sm"
            aria-pressed={isActive}
            data-testid={`category-filter-${key}`}
            onClick={() => onSelect(cat)}
            className="min-h-8 rounded-full px-3 py-1 text-xs"
          >
            {label}
          </Button>
        );
      })}
    </div>
  );
}

