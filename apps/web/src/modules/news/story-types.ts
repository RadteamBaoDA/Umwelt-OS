/** Detached owner-facing News evidence and score contracts. */
export type StoryEvidence = {
  document_id: string;
  document_version_id: string;
  chunk_id: string;
  source_id: string;
  source_name: string;
  source_type: string;
  provider: string | null;
  url: string | null;
  title: string;
  excerpt: string;
  observed_at: string;
  published_at: string | null;
  /** Feed publisher and license text when the server supplies them; shown beside the story as attribution. */
  publisher?: string | null;
  license_label?: string | null;
};

/** Story summary and evidence use only currently visible supporting versions. */
export type Story = {
  id: string;
  title: string;
  excerpt: string;
  /** Hash identifying the translatable text; empty when the server cannot translate this story. */
  translation_revision?: string;
  summary_method: 'excerpt';
  generated: false;
  observed_at: string;
  source_count: number;
  evidence_count: number;
  evidence: StoryEvidence[];
  incomplete_reasons: string[];
  relevance: number | null;
  why_relevant: string[];
  relevance_signals: Record<string, { value: number; available: boolean; method: string; evidence_ids: string[] }>;
  relevance_weights: Record<string, number>;
  relevance_profile_revisions: Array<{ kind: string; id: string; revision: number }>;
  relevance_as_of: string | null;
  relevance_state: 'available' | 'partial' | 'unavailable';
};

/** Bounded story page with a cursor scoped to the current source/filter snapshot. */
export type StoryPage = {
  items: Story[];
  next_cursor: string | null;
  as_of: string;
  truncated: boolean;
  incomplete_reasons: string[];
  capability: 'available' | 'partial' | 'unavailable';
};

/** Trend threshold and evidence window details returned by the owner API. */
export type NewsTrend = {
  story_id: string;
  title: string;
  trend: 'rising';
  current_count: number;
  baseline_per_day: number;
  ratio: number | null;
  low_baseline: boolean;
  source_count: number;
  evidence: StoryEvidence[];
  incomplete_reasons: string[];
  current_window_start: string;
  current_window_end: string;
  baseline_window_start: string;
  baseline_window_end: string;
  as_of: string;
};

/** Bounded current trend response. */
export type TrendPage = { items: NewsTrend[]; as_of: string; history_days: number; truncated: boolean; incomplete: boolean; incomplete_reasons: string[] };
/** Story detail and its separate evidence continuation. */
export type StoryDetail = { story: Story | null; evidence_cursor: string | null; incomplete_reasons: string[] };
