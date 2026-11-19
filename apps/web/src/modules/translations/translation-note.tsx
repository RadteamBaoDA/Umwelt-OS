'use client';

import Link from 'next/link';
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

/** Workspace-level block codes that should be surfaced once per gadget instead of repeated on every row. */
const WORKSPACE_BLOCKER_CODES = new Set<string>(['model_capability_missing', 'privacy_blocked', 'translation_disabled']);

/**
 * True when an issue reflects a workspace configuration blocker rather than an item-specific failure.
 *
 * @param issue Translation result in pending, blocked, or failed state.
 * @returns True if the error code applies to the whole workspace/gadget.
 */
export function isWorkspaceTranslationBlocker(issue: TranslationResult | undefined): boolean {
  return Boolean(issue?.errorCode && WORKSPACE_BLOCKER_CODES.has(issue.errorCode));
}

/**
 * Resolves the optional settings destination for a configuration-related translation block code.
 *
 * @param errorCode Machine error code returned by the translation worker.
 * @returns Route href and literal `translations` catalog key for the action link, or null.
 */
export function translationSettingsHint(errorCode: string | null | undefined): {
  href: string;
  labelKey: 'control.openAiSettings' | 'control.openPrivacySettings' | 'control.openSettings';
} | null {
  if (errorCode === 'model_capability_missing') return { href: '/settings/ai', labelKey: 'control.openAiSettings' };
  if (errorCode === 'privacy_blocked') return { href: '/settings/privacy', labelKey: 'control.openPrivacySettings' };
  if (errorCode === 'translation_disabled') return { href: '/settings/translation', labelKey: 'control.openSettings' };
  return null;
}

/**
 * One short line saying why a story/brief is still shown in its original language (pending, blocked or failed).
 * Configuration blockers include a direct link to the relevant Settings page.
 *
 * @param props.issue Current non-ready translation result, or undefined to render nothing.
 */
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
  const hint = issue.status === 'pending' ? null : translationSettingsHint(issue.errorCode);
  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
      <span>{text}</span>
      {hint ? (
        <Link href={hint.href} className="font-medium text-foreground underline underline-offset-2">
          {t(hint.labelKey)}
        </Link>
      ) : null}
    </span>
  );
}
