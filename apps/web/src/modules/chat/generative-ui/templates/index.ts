/**
 * Barrel exports for Umwelt-OS Generative UI starter templates, demo gallery,
 * explainer cards, and unified empty-state starter gallery.
 */

export {
  SEED_TEMPLATES,
  TEMPLATE_CATEGORIES,
  TEMPLATE_CATEGORY_LABELS,
  filterSeedTemplates,
  getLocalizedTemplate,
  type LocalizedSeedTemplate,
  type SeedTemplate,
  type TemplateCategory,
} from './seed-templates';

export {
  TemplateCard,
  type TemplateCardProps,
} from './template-card';

export {
  TemplateChip,
  type TemplateChipProps,
} from './template-chip';

export {
  DEMO_CATEGORIES,
  DEMO_EXAMPLES,
  getDemoCategoryLabel,
  type DemoCategory,
  type DemoItem,
} from './demo-data';

export {
  CategoryFilter,
  type CategoryFilterProps,
} from './category-filter';

export {
  DemoCard,
  type DemoCardProps,
} from './demo-card';

export {
  EXPLAINER_PILLARS,
  ExplainerCards,
  GenerativeUIStarterGallery,
  type ExplainerCardsProps,
  type ExplainerPillar,
  type GenerativeUIStarterGalleryProps,
} from './explainer-cards';

