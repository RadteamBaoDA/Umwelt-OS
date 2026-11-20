'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Languages } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Spinner } from '@/components/ui/spinner';
import { Switch } from '@/components/ui/switch';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useWorkspace } from '@/core/workspace-context';
import { fetchTranslationSettings, saveTranslationSettings } from './api';
import { TranslatedBadge } from './translated-badge';
import { isWorkspaceTranslationBlocker, TranslationNote } from './translation-note';
import type { TranslationLanguage, TranslationResult } from './types';
import type { ItemTranslationController } from './use-item-translation';
import { untranslatedTargets, type TranslationTarget } from './use-content-translation';
import { TranslationRunProgress } from './translation-progress';
import { usePendingStage } from './use-pending-stage';

/**
 * Reusable on-demand Translate button for gadgets, rows, and detail views.
 * Shows a spinner while `pending` and stays disabled until the in-flight request settles.
 *
 * @param props.label Visible button text (for example "Translate" or "Translate to Vietnamese").
 * @param props.pending True while the target translation request is in flight.
 * @param props.onClick Invoked when the user explicitly requests translation.
 * @param props.compact Renders a slimmer inline button suitable for dense list rows and toolbars.
 * @param props.ariaLabel Optional explicit accessible name when `label` is terse.
 * @param props.pendingLabel Static text shown while pending (the collection progress label); when omitted, a
 * single pending request walks the staged reassurance labels (8 s, 30 s) driven by `usePendingStage`.
 */
export function TranslateActionButton({
  label,
  pending = false,
  onClick,
  compact = false,
  ariaLabel,
  pendingLabel,
}: {
  label: string;
  pending?: boolean;
  onClick: () => void;
  compact?: boolean;
  ariaLabel?: string;
  pendingLabel?: string;
}) {
  const t = useTranslations('translations');
  const stage = usePendingStage(pending);
  // The three stage labels share one grid cell: the inactive ones are invisible and aria-hidden, so the button keeps
  // its width when the stage advances and the accessible name tracks only the visible stage.
  const stagedLabels = [t('translating'), t('stillTranslating'), t('waitingInQueue')];
  return (
    <Button
      type="button"
      variant={compact ? 'ghost' : 'outline'}
      size="sm"
      disabled={pending}
      aria-busy={pending || undefined}
      aria-label={ariaLabel}
      onClick={onClick}
      className={
        compact ? 'h-6 gap-1 px-1.5 text-xs font-normal text-muted-foreground hover:text-foreground' : 'h-7 gap-1.5 px-2.5 text-xs'
      }
    >
      {pending ? <Spinner className="size-3.5" /> : <Languages className="size-3.5" aria-hidden="true" />}
      {pending && !pendingLabel ? (
        <span className="inline-grid">
          {stagedLabels.map((text, index) => (
            <span
              key={index}
              aria-hidden={index !== stage || undefined}
              className={index === stage ? 'col-start-1 row-start-1' : 'invisible col-start-1 row-start-1'}
            >
              {text}
            </span>
          ))}
        </span>
      ) : (
        <span className="tabular-nums">{pending ? pendingLabel : label}</span>
      )}
    </Button>
  );
}

/**
 * Per-item on-demand Translate button or Original/Translation flip control for list rows and cards.
 * Renders nothing when workspace translation is disabled or the item lacks a revision. An item the server
 * reported as already in the target language shows a note instead of a dead button. A per-item reason
 * (rate limit, stale, failed) is shown under the button; workspace-wide blockers are left to the banner.
 *
 * @param props.controller Shared collection translation controller from `useItemTranslation`.
 * @param props.target Resource id and revision for this item.
 * @param props.title Item title; when given, the Translate button's accessible name includes it.
 */
export function ItemTranslateButton({
  controller,
  target,
  title,
}: {
  controller: ItemTranslationController;
  target: TranslationTarget | null | undefined;
  title?: string;
}) {
  const t = useTranslations('translations');
  if (!controller.enabled || !target?.id || !target.revision) return null;
  if (controller.results.has(target.id)) {
    const showingOriginal = controller.isShowingOriginal(target.id);
    return (
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-pressed={!showingOriginal}
        className="h-6 px-1.5 text-xs font-normal text-muted-foreground hover:text-foreground"
        onClick={() => controller.toggleItemOriginal(target.id)}
      >
        {showingOriginal ? t('translationShort') : t('originalShort')}
      </Button>
    );
  }
  if (controller.unchanged.has(target.id)) {
    return <span className="text-xs text-muted-foreground">{t('note.unchanged')}</span>;
  }
  const issue = controller.issues.get(target.id);
  return (
    <>
      <TranslateActionButton
        compact
        label={t('translate')}
        ariaLabel={title ? t('translateItem', { title }) : undefined}
        pending={controller.pendingIds.has(target.id)}
        onClick={() => controller.translateOne(target)}
      />
      {issue && issue.status !== 'pending' && !isWorkspaceTranslationBlocker(issue) ? <TranslationNote issue={issue} /> : null}
    </>
  );
}

/**
 * Compact collection-level toolbar control: shows a `Translate all` button for remaining untranslated
 * items and a `TranslatedBadge` toggle once at least one item has a ready translation.
 *
 * @param props.controller Shared collection translation controller from `useItemTranslation`.
 */
export function CollectionTranslationBar({ controller }: { controller: ItemTranslationController }) {
  const t = useTranslations('translations');
  const pendingCount = controller.pendingIds.size;
  const untranslatedCount = untranslatedTargets(controller.targets, controller).length;
  if (!controller.enabled && controller.results.size === 0) return null;
  if (controller.results.size === 0 && untranslatedCount === 0 && pendingCount === 0) return null;
  const { progress } = controller;
  return (
    <div className="relative flex flex-wrap items-center gap-1.5">
      {controller.results.size > 0 ? (
        <TranslatedBadge
          showOriginal={controller.showOriginal}
          targetLanguage={controller.targetLanguage}
          onToggle={controller.toggleAllOriginal}
        />
      ) : null}
      {controller.enabled && (untranslatedCount > 0 || pendingCount > 0) ? (
        <TranslateActionButton
          compact
          label={t('translateAll')}
          pending={pendingCount > 0}
          pendingLabel={progress ? t('translatingProgress', { done: progress.done, total: progress.total }) : undefined}
          onClick={controller.translateAll}
        />
      ) : null}
      <TranslationRunProgress progress={progress} />
    </div>
  );
}

/**
 * Deduplicated top-of-gadget banner for workspace-level translation blockers (such as missing model
 * capability or privacy policy blocks) so the same message is not repeated under every story row.
 *
 * @param props.issues Current non-ready translation outcomes keyed by resource id.
 */
export function TranslationStatusBanner({ issues }: { issues: ReadonlyMap<string, TranslationResult> }) {
  let blocker: TranslationResult | undefined;
  for (const issue of issues.values()) {
    if (isWorkspaceTranslationBlocker(issue)) {
      blocker = issue;
      break;
    }
  }
  if (!blocker) return null;
  return (
    <div role="status" className="rounded-md border border-border bg-muted/30 px-2.5 py-1.5">
      <TranslationNote issue={blocker} />
    </div>
  );
}

/** Popover form body for workspace translation toggle, target language, and error/role feedback. */
function TranslationControlFields({
  enabled,
  language,
  disabled,
  loadFailed,
  failure,
  isOwner,
  onChange,
}: {
  enabled: boolean;
  language: TranslationLanguage;
  disabled: boolean;
  loadFailed: boolean;
  failure: string | null;
  isOwner: boolean;
  onChange: (next: { enabled: boolean; target_language: TranslationLanguage }) => void;
}) {
  const t = useTranslations('translations');
  if (loadFailed) {
    return (
      <p role="alert" className="m-0 text-xs text-destructive">
        {t('loadFailed')}
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <label htmlFor="dash-translation-toggle" className="text-xs font-medium text-foreground">
          {t('enable')}
        </label>
        <Switch
          id="dash-translation-toggle"
          checked={enabled}
          disabled={disabled}
          onCheckedChange={(checked) => onChange({ enabled: checked, target_language: language })}
        />
      </div>
      <div className="space-y-1">
        <label htmlFor="dash-translation-lang" className="block text-xs font-medium text-muted-foreground">
          {t('targetLanguage')}
        </label>
        <Select
          value={language}
          disabled={disabled}
          onValueChange={(value) => onChange({ enabled, target_language: value as TranslationLanguage })}
        >
          <SelectTrigger id="dash-translation-lang" className="h-8 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="vi">{t('languageVi')}</SelectItem>
            <SelectItem value="en">{t('languageEn')}</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {failure ? (
        <p role="alert" className="m-0 text-xs text-destructive">
          {failure}
        </p>
      ) : null}
      {!isOwner ? <p className="m-0 text-xs text-muted-foreground">{t('ownerOnly')}</p> : null}
    </div>
  );
}

/**
 * Dashboard header popover showing workspace content-translation status and target language.
 * Workspace owners can toggle translation on/off and switch target language (`vi` / `en`) via CAS
 * updates synced with `/settings/translation`; non-owner members see read-only status.
 */
export function DashboardTranslationControl() {
  const t = useTranslations('translations');
  const { csrfToken } = useWorkspaceSession();
  const { isOwner, selection } = useWorkspace();
  const client = useQueryClient();
  const workspaceId = selection?.id ?? null;
  const queryKey = ['translation-settings', workspaceId];
  const [open, setOpen] = useState(false);

  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => fetchTranslationSettings(signal),
    staleTime: 30_000,
    enabled: Boolean(workspaceId),
  });

  const save = useMutation({
    mutationFn: (next: { enabled: boolean; target_language: TranslationLanguage }) =>
      saveTranslationSettings(
        { enabled: next.enabled, target_language: next.target_language, expected_revision: query.data?.configuration_revision ?? 1 },
        csrfToken,
      ),
    onSuccess: (updated) => client.setQueryData(queryKey, updated),
    onError: () => void query.refetch(),
  });

  const enabled = query.data?.enabled === true;
  const language: TranslationLanguage = query.data?.target_language ?? 'vi';
  const statusText = enabled ? language.toUpperCase() : t('control.off');
  const failure = save.error instanceof ApiError && save.error.status === 409 ? t('stale') : save.error ? t('saveFailed') : null;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button type="button" variant="outline" aria-label={`${t('control.label')}: ${statusText}`} className="gap-1.5">
          <Languages className="size-4" aria-hidden="true" />
          <span>{`${t('control.label')}: ${statusText}`}</span>
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 space-y-3 p-4">
        <div className="space-y-1">
          <h3 className="m-0 text-sm font-semibold text-foreground">{t('control.title')}</h3>
          <p className="m-0 text-xs text-muted-foreground">{t('control.description')}</p>
        </div>
        <TranslationControlFields
          enabled={enabled}
          language={language}
          disabled={!isOwner || !query.data || save.isPending}
          loadFailed={query.isError}
          failure={failure}
          isOwner={isOwner}
          onChange={(next) => save.mutate(next)}
        />
        <div className="border-t border-border pt-2">
          <Link
            href="/settings/translation"
            onClick={() => setOpen(false)}
            className="text-xs font-medium text-foreground underline underline-offset-4"
          >
            {t('control.openSettings')}
          </Link>
        </div>
      </PopoverContent>
    </Popover>
  );
}
