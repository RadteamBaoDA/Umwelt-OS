import type { AppLocaleId } from '@/core/i18n';
import {
  SEED_TEMPLATES,
  TEMPLATE_CATEGORIES,
  TEMPLATE_CATEGORY_LABELS,
  type SeedTemplate,
  type TemplateCategory,
} from './seed-templates';

/** Gallery category type mirroring the five canonical starter categories. */
export type DemoCategory = TemplateCategory;

/** Ordered list of gallery categories. */
export const DEMO_CATEGORIES: readonly DemoCategory[] = TEMPLATE_CATEGORIES;

/** Demo gallery item descriptor with bilingual copy and prompt. */
export interface DemoItem {
  /** Unique demo identifier. */
  id: string;
  /** Functional category of the demo. */
  category: DemoCategory;
  /** Localized title per application locale. */
  title: Record<AppLocaleId, string>;
  /** Localized description per application locale. */
  description: Record<AppLocaleId, string>;
  /** Localized prompt per application locale. */
  prompt: Record<AppLocaleId, string>;
  /** Expected widget kind rendered by the demo. */
  widgetKind: SeedTemplate['widgetKind'];
}

/** Curated demo gallery items derived from SEED_TEMPLATES plus additional capability showcases. */
export const DEMO_EXAMPLES: DemoItem[] = [
  ...SEED_TEMPLATES,
  {
    id: 'demo-meeting-scheduler',
    category: 'planning',
    widgetKind: 'meeting_time_picker',
    title: {
      'en-us': 'Interactive Meeting Slot Picker',
      'vi-vi': 'Chọn Khung Giờ Họp Tương tác',
    },
    description: {
      'en-us': 'Propose candidate meeting slots and confirm availability directly inside the transcript.',
      'vi-vi': 'Đề xuất các khung giờ họp và xác nhận lịch trực tiếp ngay trong cuộc trò chuyện.',
    },
    prompt: {
      'en-us':
        'Propose three 30-minute meeting slots this week for our architecture review using the interactive time slot picker.',
      'vi-vi':
        'Đề xuất 3 khung giờ họp 30 phút trong tuần này để rà soát kiến trúc bằng bộ chọn khung giờ tương tác.',
    },
  },
];

/**
 * Returns the localized label for a demo category or the 'all' filter option.
 *
 * @param category - Category identifier or null/'all'.
 * @param locale - Application locale ('en-us' | 'vi-vi').
 * @returns Localized category label.
 */
export function getDemoCategoryLabel(
  category: DemoCategory | null | 'all',
  locale: AppLocaleId = 'en-us',
): string {
  const key = !category ? 'all' : category;
  return TEMPLATE_CATEGORY_LABELS[key][locale];
}

