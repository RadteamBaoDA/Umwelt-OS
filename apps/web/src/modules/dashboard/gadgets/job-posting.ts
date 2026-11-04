import { safeHttpUrl } from '@/core/safe-url';
/** Normalized job posting row shown by the Jobs gadget; unknown fields are null. */
export interface JobPosting {
  title: string;
  company: string | null;
  location: string | null;
  salary: string | null;
  level: string | null;
  url: string | null;
  postedAt: string | null;
  sourceLabel: string | null;
}

/** Minimal projection fields `parseJobPosting` reads (structurally a subset of GadgetDocumentProjection). */
export interface JobPostingInput {
  title: string;
  canonical_url: string | null;
  published_at: string | null;
  observed_at: string | null;
  excerpt?: string | null;
  provider_metadata?: { provider?: string; source_fields?: Record<string, unknown> } | null;
}

const NOT_DISCLOSED = /^(not\s+disclosed|undisclosed|negotiable|n\/a|-)$/i;

/** Returns the trimmed value, or null when empty. */
function clean(value: string | undefined): string | null {
  const text = value?.trim();
  return text ? text : null;
}

/**
 * Parses `"Label: value; Label: value"` summaries (VietnamWorks, ITviec) into a lowercase-label map.
 * Only the first occurrence of a label wins; segments without a colon are ignored.
 *
 * @param text Excerpt or summary text; may be empty.
 */
function parseFields(text: string): Map<string, string> {
  const fields = new Map<string, string>();
  for (const part of text.split(';')) {
    const at = part.indexOf(':');
    if (at <= 0) continue;
    const label = part.slice(0, at).trim().toLowerCase();
    const value = part.slice(at + 1).trim();
    if (label && value && !fields.has(label)) fields.set(label, value);
  }
  return fields;
}

/**
 * Turns one collected job document into a table row. Company/location/salary/level come from the
 * provider summary (`Company: X; Location: Y; ...`); when no company field exists (We Work Remotely)
 * a `"Company: Title"` headline is split. Pure; never throws.
 *
 * @param item Document projection of a job datasource.
 * @returns Normalized posting with null for anything not found; undisclosed salary is null.
 */
export function parseJobPosting(item: JobPostingInput): JobPosting {
  const sourceFields = item.provider_metadata?.source_fields ?? {};
  const summary = typeof sourceFields.summary === 'string' ? sourceFields.summary : '';
  const fields = parseFields(item.excerpt?.includes(':') ? item.excerpt : summary);
  let title = item.title.trim();
  let company = clean(fields.get('company'));
  if (!company) {
    // WWR headlines read "Company: Job title"; split at the first colon only.
    const at = title.indexOf(':');
    if (at > 0 && at < title.length - 1) {
      company = clean(title.slice(0, at));
      title = title.slice(at + 1).trim();
    }
  }
  const salary = clean(fields.get('salary'));
  const publisher = typeof sourceFields.publisher === 'string' ? clean(sourceFields.publisher) : null;
  return {
    title,
    company,
    location: clean(fields.get('location')),
    salary: salary && !NOT_DISCLOSED.test(salary) ? salary : null,
    level: clean(fields.get('level')) ?? clean(fields.get('type')),
    // Feed-supplied URLs are untrusted: only http(s) links reach an href (no javascript:/data: schemes).
    url: safeHttpUrl(item.canonical_url) ?? null,
    postedAt: item.published_at ?? item.observed_at,
    sourceLabel: publisher ?? clean(item.provider_metadata?.provider),
  };
}
