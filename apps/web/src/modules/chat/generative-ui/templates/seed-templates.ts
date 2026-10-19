import type { AppLocaleId } from '@/core/i18n';
import { resolveWidgetLocale } from '../components/plan-card';

/** Canonical starter template categories for Umwelt-OS Generative UI. */
export const TEMPLATE_CATEGORIES = [
  'diagrams',
  'calculators',
  'charts',
  'tables',
  'planning',
] as const;

/** Union of supported starter template categories. */
export type TemplateCategory = (typeof TEMPLATE_CATEGORIES)[number];

/** Bilingual label map for each starter template category. */
export const TEMPLATE_CATEGORY_LABELS: Record<
  TemplateCategory | 'all',
  Record<AppLocaleId, string>
> = {
  all: {
    'en-us': 'All',
    'vi-vi': 'Tất cả',
  },
  diagrams: {
    'en-us': 'Diagrams',
    'vi-vi': 'Sơ đồ',
  },
  calculators: {
    'en-us': 'Calculators',
    'vi-vi': 'Máy tính',
  },
  charts: {
    'en-us': 'Charts',
    'vi-vi': 'Biểu đồ',
  },
  tables: {
    'en-us': 'Tables',
    'vi-vi': 'Bảng dữ liệu',
  },
  planning: {
    'en-us': 'Planning',
    'vi-vi': 'Kế hoạch',
  },
};

/** Bilingual starter template definition for the Generative UI template library. */
export interface SeedTemplate {
  /** Stable identifier for the starter template. */
  id: string;
  /** Functional category of the starter template. */
  category: TemplateCategory;
  /** Localized short title of the template. */
  title: Record<AppLocaleId, string>;
  /** Localized summary description explaining what the template generates. */
  description: Record<AppLocaleId, string>;
  /** Localized prompt dispatched when the user selects the template. */
  prompt: Record<AppLocaleId, string>;
  /** Target widget kind expected from the assistant. */
  widgetKind: 'sandboxed_ui' | 'plan' | 'table' | 'bar_chart' | 'pie_chart' | 'meeting_time_picker';
}

/** Resolved single-locale projection of a SeedTemplate. */
export interface LocalizedSeedTemplate {
  /** Stable identifier for the starter template. */
  id: string;
  /** Functional category of the starter template. */
  category: TemplateCategory;
  /** Localized category display label. */
  categoryLabel: string;
  /** Localized short title. */
  title: string;
  /** Localized summary description. */
  description: string;
  /** Localized prompt string. */
  prompt: string;
  /** Target widget kind expected from the assistant. */
  widgetKind: SeedTemplate['widgetKind'];
}

/** Curated starter templates covering all five Generative UI categories in en-us and vi-vi. */
export const SEED_TEMPLATES: SeedTemplate[] = [
  {
    id: 'tpl-architecture-diagram',
    category: 'diagrams',
    widgetKind: 'sandboxed_ui',
    title: {
      'en-us': 'System Architecture Flow',
      'vi-vi': 'Sơ đồ Kiến trúc Hệ thống',
    },
    description: {
      'en-us': 'Interactive SVG data-flow diagram with clickable pipeline stages and latency inspection.',
      'vi-vi': 'Sơ đồ luồng dữ liệu SVG tương tác với các giai đoạn xử lý và thông số độ trễ.',
    },
    prompt: {
      'en-us':
        'Create an interactive system architecture diagram showing document ingestion, chunking, hybrid vector-lexical indexing, and grounded citation synthesis with clickable node details.',
      'vi-vi':
        'Tạo sơ đồ kiến trúc hệ thống tương tác minh họa luồng thu thập tài liệu, phân đoạn, lập chỉ mục kết hợp và tổng hợp trích dẫn có thể nhấp vào từng nút.',
    },
  },
  {
    id: 'tpl-compound-calculator',
    category: 'calculators',
    widgetKind: 'sandboxed_ui',
    title: {
      'en-us': 'Runway & Allocation Simulator',
      'vi-vi': 'Mô phỏng Ngân sách & Phân bổ',
    },
    description: {
      'en-us': 'Sandboxed interactive calculator with sliders for monthly burn, reserve buffer, and growth rate.',
      'vi-vi': 'Công cụ tính toán sandbox với thanh trượt điều chỉnh chi phí hàng tháng, dự phòng và tăng trưởng.',
    },
    prompt: {
      'en-us':
        'Build an interactive financial runway and portfolio allocation calculator with sliders for monthly budget, reserve months, and expected yield.',
      'vi-vi':
        'Xây dựng công cụ tính toán thời gian duy trì ngân sách và phân bổ danh mục tương tác với thanh trượt điều chỉnh chi phí tháng và lợi suất kỳ vọng.',
    },
  },
  {
    id: 'tpl-quarterly-bar-chart',
    category: 'charts',
    widgetKind: 'bar_chart',
    title: {
      'en-us': 'Source Ingestion Throughput',
      'vi-vi': 'Lưu lượng Thu thập theo Nguồn',
    },
    description: {
      'en-us': 'Native Recharts bar chart comparing indexed document volume and freshness across connectors.',
      'vi-vi': 'Biểu đồ cột Recharts so sánh khối lượng tài liệu đã lập chỉ mục giữa các nguồn dữ liệu.',
    },
    prompt: {
      'en-us':
        'Render a bar chart comparing document ingestion throughput across our active knowledge sources over the last four weeks.',
      'vi-vi':
        'Hiển thị biểu đồ cột so sánh lưu lượng tài liệu thu thập qua các nguồn tri thức đang hoạt động trong 4 tuần qua.',
    },
  },
  {
    id: 'tpl-portfolio-pie-chart',
    category: 'charts',
    widgetKind: 'pie_chart',
    title: {
      'en-us': 'Knowledge Domain Breakdown',
      'vi-vi': 'Phân bổ Lĩnh vực Tri thức',
    },
    description: {
      'en-us': 'Native Recharts pie chart with accessible table summarizing topic distribution.',
      'vi-vi': 'Biểu đồ tròn Recharts kèm bảng dữ liệu trợ năng tóm tắt tỷ trọng chủ đề tri thức.',
    },
    prompt: {
      'en-us':
        'Show a pie chart breaking down my indexed documents and entities by topic domain with percentage shares.',
      'vi-vi':
        'Hiển thị biểu đồ tròn phân bổ tỷ trọng tài liệu và thực thể theo từng nhóm chủ đề kèm phần trăm.',
    },
  },
  {
    id: 'tpl-connector-table',
    category: 'tables',
    widgetKind: 'table',
    title: {
      'en-us': 'Connector Health & Freshness Catalog',
      'vi-vi': 'Bảng Trạng thái & Độ mới Nguồn tin',
    },
    description: {
      'en-us': 'Sortable, filterable data table with status badges, item counts, and per-row analysis actions.',
      'vi-vi': 'Bảng dữ liệu hỗ trợ sắp xếp, tìm kiếm nhanh, nhãn trạng thái và nút phân tích từng dòng.',
    },
    prompt: {
      'en-us':
        'Generate a structured comparison table of active data sources with status badges, document counts, and sync latency.',
      'vi-vi':
        'Tạo bảng dữ liệu so sánh các nguồn thu thập đang hoạt động gồm nhãn trạng thái, số lượng tài liệu và độ trễ đồng bộ.',
    },
  },
  {
    id: 'tpl-research-plan',
    category: 'planning',
    widgetKind: 'plan',
    title: {
      'en-us': 'Multi-Step Research Execution Plan',
      'vi-vi': 'Kế hoạch Nghiên cứu Đa bước',
    },
    description: {
      'en-us': 'Interactive step-by-step execution checklist with progress tracking and one-click approval.',
      'vi-vi': 'Danh sách các bước thực thi tương tác có theo dõi tiến độ và phê duyệt nhanh.',
    },
    prompt: {
      'en-us':
        'Draft a step-by-step execution plan to audit recent timeline events, cross-reference entity citations, and compile a weekly brief.',
      'vi-vi':
        'Lập kế hoạch thực thi từng bước để rà soát sự kiện gần đây, đối chiếu trích dẫn thực thể và tổng hợp bản tin tuần.',
    },
  },
];

/**
 * Resolves a SeedTemplate into localized strings for the active application locale.
 *
 * @param template - Bilingual SeedTemplate entry.
 * @param locale - Optional application locale ('en-us' | 'vi-vi').
 * @returns LocalizedSeedTemplate with resolved title, description, prompt, and categoryLabel.
 */
export function getLocalizedTemplate(
  template: SeedTemplate,
  locale?: AppLocaleId,
): LocalizedSeedTemplate {
  const resolvedLocale = resolveWidgetLocale(locale);
  return {
    id: template.id,
    category: template.category,
    categoryLabel: TEMPLATE_CATEGORY_LABELS[template.category][resolvedLocale],
    title: template.title[resolvedLocale],
    description: template.description[resolvedLocale],
    prompt: template.prompt[resolvedLocale],
    widgetKind: template.widgetKind,
  };
}

/**
 * Filters SEED_TEMPLATES by category, returning all templates when category is null or 'all'.
 *
 * @param category - Optional category filter.
 * @returns Matching SeedTemplate array.
 */
export function filterSeedTemplates(
  category?: TemplateCategory | 'all' | null,
): SeedTemplate[] {
  if (!category || category === 'all') {
    return SEED_TEMPLATES;
  }
  return SEED_TEMPLATES.filter((tpl) => tpl.category === category);
}

