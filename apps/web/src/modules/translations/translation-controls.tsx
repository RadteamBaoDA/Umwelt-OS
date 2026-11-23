'use client';

import { Languages } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { TranslatedBadge } from './translated-badge';
import { isWorkspaceTranslationBlocker, TranslationNote } from './translation-note';
import type { TranslationResult } from './types';
import type { ItemTranslationController } from './use-item-translation';
import { type TranslationTarget } from './use-content-translation';
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
 * @param props.className Extra classes appended to the button (used by row reveal to hide an idle button).
 */
export function TranslateActionButton({
  label,
  pending = false,
  onClick,
  compact = false,
  ariaLabel,
  pendingLabel,
  className,
}: {
  label: string;
  pending?: boolean;
  onClick: () => void;
  compact?: boolean;
  ariaLabel?: string;
  pendingLabel?: string;
  className?: string;
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
      className={[
        compact ? 'h-6 gap-1 px-1.5 text-xs font-normal text-muted-foreground hover:text-foreground' : 'h-7 gap-1.5 px-2.5 text-xs',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
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
 * Row reveal classes for an idle per-item Translate button: hidden until its row (`group/item`) is hovered or
 * holds focus, always shown on coarse pointers, and always shown on keyboard focus of the button itself.
 * Pending and issue states bypass this class so progress and errors are never hidden.
 */
export const ITEM_REVEAL_CLASS =
  'opacity-0 transition-opacity group-hover/item:opacity-100 group-focus-within/item:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100';

/**
 * Per-item on-demand Translate button or Original/Translation flip control for list rows and cards.
 * Renders nothing when workspace translation is disabled or the item lacks a revision. An item the server
 * reported as already in the target language shows a note instead of a dead button. A per-item reason
 * (rate limit, stale, failed) is shown under the button; workspace-wide blockers are left to the banner.
 * The untranslated Translate button is revealed on row hover/focus (the row must carry `group/item`); the
 * Original/Translation toggle and any pending or issue state stay always visible.
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
  const pending = controller.pendingIds.has(target.id);
  // Pending or failed items keep the button visible so the user sees progress or the reason without hovering.
  const revealed = pending || Boolean(issue);
  return (
    <>
      <TranslateActionButton
        compact
        label={t('translate')}
        ariaLabel={title ? t('translateItem', { title }) : undefined}
        pending={pending}
        className={revealed ? undefined : ITEM_REVEAL_CLASS}
        onClick={() => controller.translateOne(target)}
      />
      {issue && issue.status !== 'pending' && !isWorkspaceTranslationBlocker(issue) ? <TranslationNote issue={issue} /> : null}
    </>
  );
}

/**
 * Compact collection-level status control: a `TranslatedBadge` original/translation toggle once at least one
 * item has a ready translation, plus batch progress while requests run. "Translate all" is no longer here; it
 * lives in the gadget's ⋯ menu (see `useTranslateAllAction`).
 *
 * @param props.controller Shared collection translation controller from `useItemTranslation`.
 */
export function CollectionTranslationBar({ controller }: { controller: ItemTranslationController }) {
  if (controller.results.size === 0 && controller.pendingIds.size === 0) return null;
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
