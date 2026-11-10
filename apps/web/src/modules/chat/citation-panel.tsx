'use client';

import * as React from 'react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { ExternalLinkIcon, FileTextIcon, GlobeIcon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import type { Citation, DocumentCitation as DocCitationData, WebCitation as WebCitationData } from './api';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime, type AppLocaleId } from '@/core/i18n';
import { safeHttpUrl } from '@/core/safe-url';

/** Host of an untrusted web URL for display; empty string when unparsable. */
export function webHost(url: string): string {
  try { return new URL(url).hostname; } catch { return ''; }
}

/**
 * Stable identity of a citation across messages: the URL for web results, version plus chunk for documents.
 * The Chat page de-duplicates the rail list with the same rule, so a chip click can find its card.
 *
 * @param citation - Any grounded citation.
 */
export function citationIdentity(citation: Citation): string {
  return citation.sourceType === 'web' ? citation.url : `${citation.documentVersionId}:${citation.chunkId}`;
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
  /** Rail cards (variant `card`) to emphasize, by {@link citationIdentity}; the first one is scrolled into view. */
  highlightedKeys?: readonly string[];
  /** Changes on each focus request so the same card scrolls into view again. */
  focusNonce?: number;
}

/** Shared formatting context of the citation renderers. */
interface CitationViewProps {
  citation: Citation;
  index: number;
  locale: AppLocaleId;
  timezone: string;
  /** Rail card emphasis requested by a citation click. */
  highlighted?: boolean;
  /** True for the single card that should scroll into view. */
  scrollTarget?: boolean;
  focusNonce?: number;
}

/** Sources rail entry (Chat.dc.html): numbered chip, title, meta, then the exact-item link. */
function CardCitation({ citation, index, locale, timezone, highlighted = false, scrollTarget = false, focusNonce = 0 }: CitationViewProps) {
  const t = useTranslations('chat');
  const cardRef = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    if (scrollTarget) cardRef.current?.scrollIntoView?.({ block: 'nearest' });
  }, [scrollTarget, focusNonce]);
  const href = citation.sourceType === 'web'
    ? safeHttpUrl(citation.url)
    : `/knowledge/documents/${encodeURIComponent(citation.documentId)}?${new URLSearchParams({ versionId: citation.documentVersionId, chunkId: citation.chunkId })}#cited-chunk`;
  const when = citation.sourceType !== 'web' && citation.observedAt ? formatDateTime(citation.observedAt, locale, timezone) : null;
  const linkClass = 'inline-flex min-h-9 pointer-coarse:min-h-11 items-center gap-1 text-[13px] font-medium text-foreground underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring';
  return (
    <div
      ref={cardRef}
      data-testid="rail-source"
      data-citation-key={citationIdentity(citation)}
      aria-current={highlighted ? 'true' : undefined}
      className={`flex flex-col gap-1 rounded-md border-b border-rule px-2 py-2 text-xs hover:bg-active focus-within:bg-active ${highlighted ? 'bg-active ring-2 ring-data' : ''}`}
    >
      <span className="flex items-center gap-2">
        <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-sm bg-raised px-[5px] text-xs font-semibold text-foreground">{index + 1}</span>
        <strong className="line-clamp-2 text-sm font-semibold text-foreground">{citation.title}</strong>
      </span>
      {citation.quote && <span className="line-clamp-1 text-xs text-muted-foreground">{citation.quote}</span>}
      <span className="text-xs text-muted-foreground">{citation.sourceType === 'web' ? webHost(citation.url) : [t('document'), when].filter(Boolean).join(' · ')}</span>
      {href && (citation.sourceType === 'web'
        ? <a href={href} target="_blank" rel="noopener noreferrer nofollow" className={linkClass} aria-label={t('openWebResultFor', { host: webHost(citation.url) })}>{t('openWebResult')}<ExternalLinkIcon className="size-3" aria-hidden="true" /></a>
        : <Link href={href} className={linkClass}>{t('openExactItem')}</Link>)}
    </div>
  );
}

/** Web result: title and snippet are untrusted third-party text, so they render as plain React text only. */
function WebCitation({ citation, index }: Omit<CitationViewProps, 'citation'> & { citation: WebCitationData }) {
  const t = useTranslations('chat');
  const href = safeHttpUrl(citation.url);
  return (
    <div className="flex flex-col gap-2 p-3 rounded-lg border border-border bg-background text-xs">
      <div className="flex items-start justify-between gap-2">
        <span className="font-semibold text-foreground line-clamp-2">[{index + 1}] {citation.title}</span>
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
          <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="inline-flex min-h-9 pointer-coarse:min-h-11 shrink-0 items-center gap-1 text-primary hover:underline" aria-label={t('openWebResultFor', { host: webHost(citation.url) })}>
            <span>{t('openWebResult')}</span><ExternalLinkIcon className="size-3" aria-hidden="true" />
          </a>
        )}
      </div>
    </div>
  );
}

/** Knowledge document citation with exact version/chunk link, optional external link, grounded quote and timestamp. */
function DocumentCitation({ citation, index, locale, timezone }: Omit<CitationViewProps, 'citation'> & { citation: DocCitationData }) {
  const t = useTranslations('chat');
  const citationQuery = new URLSearchParams({
    versionId: citation.documentVersionId,
    chunkId: citation.chunkId,
  });
  const docHref = `/knowledge/documents/${encodeURIComponent(citation.documentId)}?${citationQuery.toString()}#cited-chunk`;
  const formattedDate = citation.observedAt
    ? formatDateTime(citation.observedAt, locale, timezone)
    : null;
  const externalUrl = safeHttpUrl(citation.url);
  return (
    <div className="flex flex-col gap-2 p-3 rounded-lg border border-border bg-background text-xs">
      <div className="flex items-start justify-between gap-2">
        <span className="font-semibold text-foreground line-clamp-1">
          [{index + 1}] {citation.title}
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
          {externalUrl && (
            <a
              href={externalUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
              title={externalUrl}
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
}

/** React key of a citation entry: chunk-based for documents (and the rail), index-based for plain web results. */
function citationKey(citation: Citation, variant: CitationPanelProps['variant'], idx: number): string {
  if (variant === 'card') return `${citation.sourceType === 'web' ? 'web' : citation.chunkId}-${idx}`;
  return citation.sourceType === 'web' ? `web-${idx}` : `${citation.chunkId}-${idx}`;
}

/** Picks the renderer for one citation: rail card, web result or knowledge document. */
function CitationEntry({ variant, ...view }: CitationViewProps & { variant: CitationPanelProps['variant'] }) {
  if (variant === 'card') return <CardCitation {...view} />;
  const { citation } = view;
  return citation.sourceType === 'web'
    ? <WebCitation {...view} citation={citation} />
    : <DocumentCitation {...view} citation={citation} />;
}

/** Container classes per panel variant. */
const PANEL_CLASS = {
  standalone: 'flex flex-col gap-3 p-4 rounded-lg border border-border bg-surface',
  card: 'flex flex-col gap-1 mt-2',
  inline: 'flex flex-col gap-2 mt-2 pt-2 border-t border-border/60',
} as const;

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
  highlightedKeys,
  focusNonce,
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
    <div className={PANEL_CLASS[variant]}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-xs font-semibold text-muted-foreground">
          <FileTextIcon className="size-3.5" />
          <span>{t('citations')} ({itemsToRender.length})</span>
        </div>
        {onClose && (
          <Button type="button" variant="ghost" size="icon" onClick={onClose} className="size-11 text-muted-foreground" aria-label={t('closeDrawer')}>
            <XIcon className="size-3.5" aria-hidden="true" />
          </Button>
        )}
      </div>

      <div className="flex flex-col gap-2.5">
        {itemsToRender.map((citation, idx) => (
          <CitationEntry
            key={citationKey(citation, variant, idx)}
            variant={variant}
            citation={citation}
            index={idx}
            locale={locale}
            timezone={timezone}
            highlighted={highlightedKeys?.includes(citationIdentity(citation))}
            scrollTarget={highlightedKeys?.[0] === citationIdentity(citation)}
            focusNonce={focusNonce}
          />
        ))}
      </div>
    </div>
  );
}
