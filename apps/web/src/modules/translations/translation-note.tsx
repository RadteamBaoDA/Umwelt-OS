'use client';

import { useTranslations } from 'next-intl';
import type { TranslationResult } from './types';

const CODES = ['privacy_blocked', 'model_capability_missing', 'input_too_large', 'deadline_exceeded', 'invalid_model_output', 'attempts_exhausted', 'resource_unavailable', 'stale_request', 'translation_disabled'] as const;

/** One short line saying why a story/brief is still shown in its original language (pending, blocked or failed). */
export function TranslationNote({ issue }: { issue: TranslationResult | undefined }) {
  const t = useTranslations('translations');
  if (!issue) return null;
  const code = (CODES as readonly string[]).includes(issue.errorCode ?? '') ? (issue.errorCode as (typeof CODES)[number]) : null;
  const text = issue.status === 'pending' ? t('notePending') : code ? t(`reason_${code}`) : issue.status === 'blocked' ? t('noteBlocked') : t('noteFailed');
  return <span className="block text-xs text-muted-foreground">{text}</span>;
}
