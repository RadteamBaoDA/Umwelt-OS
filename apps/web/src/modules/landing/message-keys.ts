import type { LandingSourceId } from './source-canvas';

/** Sample question ids of the landing demo. */
export type DemoQuestion = 'latest' | 'summarize' | 'compare';

/** Source catalog group ids shown in the landing sources section. */
export type SourceGroupId =
  'web' | 'personal' | 'news' | 'research' | 'finance' | 'environment' | 'security' | 'space' | 'health' | 'jobs' | 'imports';

/**
 * Literal `landing` catalog keys for the demo's enumerable ids. Tables replace runtime key building
 * (`sourceNames.${id}`) so every key is checked against locales/en-us/landing.json at compile time.
 */
export const SOURCE_NAME_KEYS = {
  website: 'sourceNames.website',
  feed: 'sourceNames.feed',
  document: 'sourceNames.document',
} as const satisfies Record<LandingSourceId, string>;
export const SOURCE_TYPE_KEYS = {
  website: 'sourceTypes.website',
  feed: 'sourceTypes.feed',
  document: 'sourceTypes.document',
} as const satisfies Record<LandingSourceId, string>;
export const REFERENCE_TEXT_KEYS = {
  website: 'referenceText.website',
  feed: 'referenceText.feed',
  document: 'referenceText.document',
} as const satisfies Record<LandingSourceId, string>;
export const QUESTION_KEYS = {
  latest: 'questions.latest',
  summarize: 'questions.summarize',
  compare: 'questions.compare',
} as const satisfies Record<DemoQuestion, string>;
export const ANSWER_KEYS = {
  latest: 'answers.latest',
  summarize: 'answers.summarize',
  compare: 'answers.compare',
} as const satisfies Record<DemoQuestion, string>;
export const EVIDENCE_KEYS = {
  latest: {
    website: 'answerEvidenceBySource.latest.website',
    feed: 'answerEvidenceBySource.latest.feed',
    document: 'answerEvidenceBySource.latest.document',
  },
  summarize: {
    website: 'answerEvidenceBySource.summarize.website',
    feed: 'answerEvidenceBySource.summarize.feed',
    document: 'answerEvidenceBySource.summarize.document',
  },
  compare: {
    website: 'answerEvidenceBySource.compare.website',
    feed: 'answerEvidenceBySource.compare.feed',
    document: 'answerEvidenceBySource.compare.document',
  },
} as const satisfies Record<DemoQuestion, Record<LandingSourceId, string>>;
export const SOURCE_GROUP_KEYS = {
  web: 'sourceGroups.web',
  personal: 'sourceGroups.personal',
  news: 'sourceGroups.news',
  research: 'sourceGroups.research',
  finance: 'sourceGroups.finance',
  environment: 'sourceGroups.environment',
  security: 'sourceGroups.security',
  space: 'sourceGroups.space',
  health: 'sourceGroups.health',
  jobs: 'sourceGroups.jobs',
  imports: 'sourceGroups.imports',
} as const satisfies Record<SourceGroupId, string>;
