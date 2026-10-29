'use client';

import * as React from 'react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { ExternalLinkIcon, FileTextIcon, GlobeIcon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import type { Citation } from '@/modules/chat/api';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime } from '@/core/i18n';
import { safeHttpUrl } from '@/core/safe-url';

/** Host of an untrusted web URL for display; empty string when unparsable. */
export function webHost(url: string): string {
  try { return new URL(url).hostname; } catch { return ''; }
}

export interface CitationPanelProps {
  /** List of citations to render. */
  citations: Citation[];
  /** Currently selected citation to highlight, if any. */
  selectedCitation?: Citation | null;
  /** Callback when user closes or dismisses the citation inspector. */
  onClose?: () => void;
  /** Whether the panel is rendered as a standalone overlay/card or an inline list. */
  variant?: 'inline' | 'card' | 'standalone';
}

/**
 * Citation evidence inspector rendering source provenance, exact version/chunk navigation,
 * observed timestamps, and grounded quote excerpts through the owner-checked Documents reader.
 *
 * @param props - CitationPanelProps interface.
 * @returns Accessible citation list or detailed inspection panel.
 */
export function CitationPanel({
  citations,
  selectedCitation,
  onClose,
  variant = 'inline',
}: CitationPanelProps) {
  const t = useTranslations('chat');
  const display = useDisplayPreferences();
  const timezone = display.confirmedPreferences?.timezone || 'UTC';
  const locale = display.confirmedPreferences?.locale || 'en-us';

  if (!citations.length && !selectedCitation) {
    return null;
  }

  const itemsToRender = selectedCitation ? [selectedCitation] : citations;

  return (
    <div
      className={
        variant === 'standalone'
          ? 'flex flex-col gap-3 p-4 rounded-xl border border-border bg-surface shadow-md'
          : variant === 'card'
            ? 'flex flex-col gap-2 p-3 rounded-lg border border-border bg-surface/70 mt-2'
            : 'flex flex-col gap-2 mt-2 pt-2 border-t border-border/60'
      }
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wider">
          <FileTextIcon className="size-3.5 text-primary" />
          <span>{t('citations')} ({itemsToRender.length})</span>
        </div>
        {onClose && (
          <Button type="button" variant="ghost" size="icon" onClick={onClose} className="size-11 text-muted-foreground" aria-label={t('closeDrawer')}>
            <XIcon className="size-3.5" aria-hidden="true" />
          </Button>
        )}
      </div>

      <div className="flex flex-col gap-2.5">
        {itemsToRender.map((citation, idx) => {
          if (variant === 'card') {
            // Sources rail (Chat.dc.html): numbered chip, title, meta, then the exact-item link.
            const isWeb = citation.sourceType === 'web';
            const href = isWeb ? safeHttpUrl(citation.url) : `/knowledge/documents/${encodeURIComponent(citation.documentId)}?${new URLSearchParams({ versionId: citation.documentVersionId, chunkId: citation.chunkId })}#cited-chunk`;
            const when = !isWeb && citation.observedAt ? formatDateTime(citation.observedAt, locale, timezone) : null;
            const linkClass = 'inline-flex min-h-11 items-center gap-1 text-[13px] font-semibold text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring';
            return (
              <div key={`${isWeb ? 'web' : citation.chunkId}-${idx}`} className="flex flex-col gap-1 rounded-[10px] border border-border bg-background p-3 text-xs">
                <span className="flex items-center gap-2">
                  <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-md bg-primary/10 px-[5px] text-xs font-semibold text-primary">{idx + 1}</span>
                  <strong className="line-clamp-2 text-sm font-semibold text-foreground">{citation.title}</strong>
                </span>
                <span className="text-xs text-muted-foreground">{isWeb ? webHost(citation.url) : [t('document'), when].filter(Boolean).join(' · ')}</span>
                {href && (isWeb
                  ? <a href={href} target="_blank" rel="noopener noreferrer nofollow" className={linkClass} aria-label={t('openWebResultFor', { host: webHost(citation.url) })}>{t('openWebResult')}<ExternalLinkIcon className="size-3" aria-hidden="true" /></a>
                  : <Link href={href} className={linkClass}>{t('openExactItem')}</Link>)}
              </div>
            );
          }
          if (citation.sourceType === 'web') {
            const href = safeHttpUrl(citation.url);
            // Title and snippet are untrusted third-party text: plain React text only, never markdown or HTML.
            return (
              <div key={`web-${idx}`} className="flex flex-col gap-2 p-3 rounded-lg border border-border bg-background text-xs">
                <div className="flex items-start justify-between gap-2">
                  <span className="font-semibold text-foreground line-clamp-2">[{idx + 1}] {citation.title}</span>
                  <span className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-background px-2 text-xs text-muted-foreground">
                    <GlobeIcon className="size-3" aria-hidden="true" />{t('webBadge')}
                  </span>
                </div>
                {citation.quote && (
                  <p className="pl-2 border-l-2 border-primary/40 text-muted-foreground line-clamp-4 text-xs leading-relaxed">{citation.quote}</p>
                )}
                <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground pt-1 border-t border-border/40">
                  <span className="truncate">{webHost(citation.url)}</span>
                  {href && (
                    <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="inline-flex min-h-11 shrink-0 items-center gap-1 text-primary hover:underline" aria-label={t('openWebResultFor', { host: webHost(citation.url) })}>
                      <span>{t('openWebResult')}</span><ExternalLinkIcon className="size-3" aria-hidden="true" />
                    </a>
                  )}
                </div>
              </div>
            );
          }
          const citationQuery = new URLSearchParams({
            versionId: citation.documentVersionId,
            chunkId: citation.chunkId,
          });
          const docHref = `/knowledge/documents/${encodeURIComponent(citation.documentId)}?${citationQuery.toString()}#cited-chunk`;
          const formattedDate = citation.observedAt
            ? formatDateTime(citation.observedAt, locale, timezone)
            : null;

          return (
            <div
              key={`${citation.chunkId}-${idx}`}
              className="flex flex-col gap-2 p-3 rounded-lg border border-border bg-background text-xs"
            >
              <div className="flex items-start justify-between gap-2">
                <span className="font-semibold text-foreground line-clamp-1">
                  [{idx + 1}] {citation.title}
                </span>
                <div className="flex items-center gap-2 shrink-0">
                  <Link
                    href={docHref}
                    className="inline-flex items-center gap-1 text-xs text-primary hover:underline font-medium"
                    title={t('openDocument')}
                  >
                    <span>{t('document')}</span>
                    <ExternalLinkIcon className="size-3" aria-hidden="true" />
                  </Link>
                  {safeHttpUrl(citation.url) && (
                    <a
                      href={safeHttpUrl(citation.url)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                      title={safeHttpUrl(citation.url)}
                    >
                      <ExternalLinkIcon className="size-3" aria-hidden="true" />
                    </a>
                  )}
                </div>
              </div>

              {citation.quote && (
                <blockquote className="pl-2 border-l-2 border-primary/40 italic text-muted-foreground line-clamp-4 text-xs leading-relaxed">
                  &ldquo;{citation.quote}&rdquo;
                </blockquote>
              )}

              <div className="flex items-center justify-between text-xs text-muted-foreground pt-1 border-t border-border/40">
                <span>{t('source')}: {t('document')}</span>
                {formattedDate && <span>{formattedDate}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
