import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useNewsIncompleteLabel } from './use-news-incomplete-label';

vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => `news.${key}` }));

describe('useNewsIncompleteLabel', () => {
  it.each([
    ['candidate_scan_limit', 'candidateScanLimit'],
    ['support_scan_limit', 'supportScanLimit'],
    ['stale_support_omitted', 'staleSupportOmitted'],
    ['chunk_limit', 'chunkLimit'],
    ['entity_membership_limit', 'entityMembershipLimit'],
    ['source_selection_limit', 'sourceSelectionLimit'],
    ['scope_unavailable', 'scopeUnavailable'],
    ['history_scan_limit', 'historyScanLimit'],
    ['evidence_changed_during_read', 'evidenceChanged'],
  ])('maps %s to the %s message', (code, key) => {
    const { result } = renderHook(() => useNewsIncompleteLabel());
    expect(result.current(code)).toBe(`news.${key}`);
  });

  it('uses the generic notice for an unknown server code', () => {
    const { result } = renderHook(() => useNewsIncompleteLabel());
    expect(result.current('something_new')).toBe('news.unknownIncomplete');
  });
});
