'use client';

import { useTranslations } from 'next-intl';

/** Resolve stable server omission codes to translated News notices. */
export function useNewsIncompleteLabel() {
  const t = useTranslations('news');
  return (reason: string): string => {
    switch (reason) {
      case 'candidate_scan_limit': return t('candidateScanLimit');
      case 'support_scan_limit': return t('supportScanLimit');
      case 'stale_support_omitted': return t('staleSupportOmitted');
      case 'chunk_limit': return t('chunkLimit');
      case 'entity_membership_limit': return t('entityMembershipLimit');
      case 'source_selection_limit': return t('sourceSelectionLimit');
      case 'scope_unavailable': return t('scopeUnavailable');
      case 'history_scan_limit': return t('historyScanLimit');
      case 'evidence_changed_during_read': return t('evidenceChanged');
      default: return t('unknownIncomplete');
    }
  };
}
