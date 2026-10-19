'use client';

import * as React from 'react';
import { BarChart3Icon, Code2Icon, MessageSquareReplyIcon, SparklesIcon } from 'lucide-react';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import type { AppLocaleId } from '@/core/i18n';
import { chatMessages } from '@/core/messages/chat';
import { resolveWidgetLocale } from '../components/plan-card';
import { CategoryFilter } from './category-filter';
import type { DemoCategory } from './demo-data';
import { filterSeedTemplates, SEED_TEMPLATES } from './seed-templates';
import { TemplateCard } from './template-card';
import { TemplateChip } from './template-chip';

/** Descriptor for an architectural pillar explainer card. */
export interface ExplainerPillar {
  /** Stable identifier for the explainer pillar. */
  id: 'sandboxed_ui' | 'native_widgets' | 'interactive_followups';
  /** Localized title per application locale. */
  title: Record<AppLocaleId, string>;
  /** Localized description per application locale. */
  description: Record<AppLocaleId, string>;
}

/** Three core Generative UI explainer pillars in en-us and vi-vi. */
export const EXPLAINER_PILLARS: ExplainerPillar[] = [
  {
    id: 'sandboxed_ui',
    title: {
      'en-us': 'Sandboxed Interactive UI',
      'vi-vi': 'Giao diện Tương tác Sandbox',
    },
    description: {
      'en-us':
        'Custom HTML/CSS/SVG/JS diagrams and calculators stream progressively inside an isolated iframe with theme synchronization.',
      'vi-vi':
        'Sơ đồ và công cụ tính toán HTML/CSS/SVG/JS được truyền phát trực tiếp trong iframe cô lập, đồng bộ giao diện sáng/tối.',
    },
  },
  {
    id: 'native_widgets',
    title: {
      'en-us': 'Native Structured Widgets',
      'vi-vi': 'Tiện ích Cấu trúc Nguyên bản',
    },
    description: {
      'en-us':
        'Plans, sortable tables, Recharts bar/pie charts, and meeting pickers render with native Umwelt-OS tokens and accessibility.',
      'vi-vi':
        'Kế hoạch, bảng dữ liệu, biểu đồ Recharts và bộ chọn lịch hiển thị bằng thành phần chuẩn Umwelt-OS kèm bảng trợ năng.',
    },
  },
  {
    id: 'interactive_followups',
    title: {
      'en-us': 'Two-Way Follow-Up Actions',
      'vi-vi': 'Tương tác Phản hồi Hai chiều',
    },
    description: {
      'en-us':
        'Approving plans, analyzing table rows, or clicking sandboxed controls sends structured follow-up prompts back to the assistant.',
      'vi-vi':
        'Phê duyệt kế hoạch, phân tích dòng dữ liệu hoặc thao tác trên tiện ích sẽ gửi yêu cầu tiếp nối trực tiếp tới trợ lý.',
    },
  },
];

/** Props accepted by the ExplainerCards component. */
export interface ExplainerCardsProps {
  /** Application locale ('en-us' | 'vi-vi'). */
  locale?: AppLocaleId;
}

/** Props accepted by the unified GenerativeUIStarterGallery component. */
export interface GenerativeUIStarterGalleryProps {
  /** Callback invoked when the user clicks a starter template card or quick-action chip. */
  onSelectPrompt: (prompt: string) => void;
  /** Application locale ('en-us' | 'vi-vi'). */
  locale?: AppLocaleId;
  /** Optional compact mode for drawer surfaces (defaults to false). */
  compact?: boolean;
}

/**
 * Renders the icon corresponding to an ExplainerPillar identifier.
 *
 * @param id - ExplainerPillar ID.
 * @returns Lucide icon element.
 */
function renderPillarIcon(id: ExplainerPillar['id']) {
  switch (id) {
    case 'sandboxed_ui':
      return <Code2Icon className="size-4" aria-hidden="true" />;
    case 'native_widgets':
      return <BarChart3Icon className="size-4" aria-hidden="true" />;
    case 'interactive_followups':
      return <MessageSquareReplyIcon className="size-4" aria-hidden="true" />;
  }
}

/**
 * ExplainerCards component summarizing how Sandboxed UI, Native Structured Widgets,
 * and Two-Way Interactive Follow-ups work in Umwelt-OS Chat.
 *
 * @param props - ExplainerCardsProps with optional locale.
 * @returns Grid of three pillar explainer cards.
 */
export function ExplainerCards({ locale }: ExplainerCardsProps) {
  const resolvedLocale = resolveWidgetLocale(locale);

  return (
    <div
      data-testid="generative-explainer-cards"
      className="grid grid-cols-1 gap-3 sm:grid-cols-3"
    >
      {EXPLAINER_PILLARS.map((pillar) => (
        <Card
          key={pillar.id}
          data-testid={`explainer-card-${pillar.id}`}
          className="gap-2 border-border bg-surface py-3.5 text-foreground shadow-xs"
        >
          <CardHeader className="gap-1.5 px-4 pb-0">
            <div className="flex items-center gap-2">
              <span className="inline-flex size-7 items-center justify-center rounded-md bg-primary/10 text-primary">
                {renderPillarIcon(pillar.id)}
              </span>
              <CardTitle className="text-xs font-semibold text-foreground">
                {pillar.title[resolvedLocale]}
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent className="px-4 py-0">
            <CardDescription className="text-[11px] leading-relaxed text-muted-foreground">
              {pillar.description[resolvedLocale]}
            </CardDescription>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

/**
 * Unified starter gallery for the empty state of ChatTranscript.
 * Combines ExplainerCards, CategoryFilter, quick-action TemplateChips, and filterable TemplateCards.
 *
 * @param props - GenerativeUIStarterGalleryProps configuration and prompt callback.
 * @returns Accessible starter gallery section.
 */
export function GenerativeUIStarterGallery({
  onSelectPrompt,
  locale,
  compact = false,
}: GenerativeUIStarterGalleryProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const catalog = chatMessages[resolvedLocale];
  const [selectedCategory, setSelectedCategory] = React.useState<DemoCategory | null>(null);

  const visibleTemplates = React.useMemo(
    () => filterSeedTemplates(selectedCategory),
    [selectedCategory],
  );

  return (
    <section
      aria-label={catalog.genUiStarterTitle}
      data-testid="generative-ui-starter-gallery"
      className="mx-auto mt-4 w-full max-w-3xl space-y-4 text-left"
    >
      <div className="flex flex-col items-center text-center">
        <div className="inline-flex items-center gap-1.5 rounded-full border border-border bg-background px-3 py-1 text-xs font-semibold text-primary">
          <SparklesIcon className="size-3.5" aria-hidden="true" />
          <span>{catalog.genUiStarterTitle}</span>
        </div>
        <p className="mt-1.5 max-w-xl text-xs text-muted-foreground">
          {catalog.genUiStarterSubtitle}
        </p>
      </div>

      {!compact && <ExplainerCards locale={resolvedLocale} />}

      <div className="flex flex-wrap items-center justify-center gap-2">
        <CategoryFilter
          selected={selectedCategory}
          onSelect={setSelectedCategory}
          locale={resolvedLocale}
        />
      </div>

      {compact ? (
        <div className="flex flex-wrap items-center justify-center gap-2">
          {SEED_TEMPLATES.slice(0, 4).map((tpl) => (
            <TemplateChip
              key={tpl.id}
              template={tpl}
              locale={resolvedLocale}
              onSelectPrompt={onSelectPrompt}
            />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {visibleTemplates.map((tpl) => (
            <TemplateCard
              key={tpl.id}
              template={tpl}
              locale={resolvedLocale}
              onSelectPrompt={(prompt) => onSelectPrompt(prompt)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

