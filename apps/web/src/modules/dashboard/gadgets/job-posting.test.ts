import { describe, expect, it } from 'vitest';
import { parseJobPosting } from './job-posting';

const base = { canonical_url: 'https://x/1', published_at: '2026-10-01T00:00:00Z', observed_at: '2026-10-02T00:00:00Z' };

describe('parseJobPosting', () => {
  it('parses VietnamWorks summary fields', () => {
    const p = parseJobPosting({ ...base, title: 'Backend Dev', excerpt: 'Company: Acme; Level: Senior; Location: Hanoi, HCM; Salary: $2000 - $3000; Skills: Go',
      provider_metadata: { provider: 'vietnamworks_jobs', source_fields: { publisher: 'VietnamWorks' } } });
    expect(p).toMatchObject({ title: 'Backend Dev', company: 'Acme', level: 'Senior', location: 'Hanoi, HCM', salary: '$2000 - $3000', sourceLabel: 'VietnamWorks', postedAt: base.published_at });
  });
  it('parses ITviec type and nulls undisclosed salary', () => {
    const p = parseJobPosting({ ...base, title: 'Dev', excerpt: 'Company: Beta; Type: Hybrid; Location: HCM; Salary: Not disclosed',
      provider_metadata: { provider: 'itviec_jobs', source_fields: {} } });
    expect(p).toMatchObject({ company: 'Beta', level: 'Hybrid', salary: null, sourceLabel: 'itviec_jobs' });
  });
  it('splits a WWR "Company: Title" headline', () => {
    const p = parseJobPosting({ ...base, title: 'Example Co: Senior Backend Engineer', excerpt: 'Build services.', provider_metadata: { provider: 'wwr_jobs', source_fields: { publisher: 'We Work Remotely' } } });
    expect(p).toMatchObject({ company: 'Example Co', title: 'Senior Backend Engineer', location: null, salary: null });
  });
  it('falls back to summary and observed_at; missing fields are null', () => {
    const p = parseJobPosting({ ...base, published_at: null, title: 'Plain', excerpt: '', provider_metadata: { provider: 'x', source_fields: { summary: 'Company: Gamma' } } });
    expect(p).toMatchObject({ company: 'Gamma', location: null, level: null, postedAt: base.observed_at });
  });
  it('drops non-http links from feed data', () => {
    const p = parseJobPosting({ ...base, canonical_url: 'javascript:alert(1)', title: 'X', excerpt: '', provider_metadata: null });
    expect(p.url).toBeNull();
  });
});
