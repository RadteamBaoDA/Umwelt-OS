'use client';

import { useTranslations } from 'next-intl';
import type { TranslationResult } from './types';

/** Literal catalog key per translation failure code (no runtime key building). */
const REASON_KEYS = {
  privacy_blocked: 'reason.privacyBlocked',
  model_capability_missing: 'reason.modelCapabilityMissing',
  input_too_large: 'reason.inputTooLarge',
  deadline_exceeded: 'reason.deadlineExceeded',
  invalid_model_output: 'reason.invalidModelOutput',
  attempts_exhausted: 'reason.attemptsExhausted',
  resource_unavailable: 'reason.resourceUnavailable',
  stale_request: 'reason.staleRequest',
  translation_disabled: 'reason.translationDisabled',
} as const;
const CODES = Object.keys(REASON_KEYS) as (keyof typeof REASON_KEYS)[];

/** One short line saying why a story/brief is still shown in its original language (pending, blocked or failed). */
export function TranslationNote({ issue }: { issue: TranslationResult | undefined }) {
  const t = useTranslations('translations');
  if (!issue) return null;
  const code = CODES.find((known) => known === issue.errorCode) ?? null;
  const text =
    issue.status === 'pending'
      ? t('note.pending')
      : code
        ? t(REASON_KEYS[code])
        : issue.status === 'blocked'
          ? t('note.blocked')
          : t('note.failed');
  return <span className="block text-xs text-muted-foreground">{text}</span>;
}
