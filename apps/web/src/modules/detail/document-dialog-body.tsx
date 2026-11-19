'use client';

import Link from 'next/link';
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ExternalLink } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { formatDateTime, type AppLocaleId } from '@/core/i18n';
import { safeHttpUrl } from '@/core/safe-url';
import { useDisplayPreferences } from '@/core/query-provider';
import { documentKeys, getDocument, getVersion } from '@/modules/knowledge';
import { TranslateActionButton, TranslatedBadge, TranslationNote, useContentTranslation } from '@/modules/translations';

const PREVIEW_LIMIT = 2000;

/** On-demand translation button, badge, or failure note for a single opened document detail. */
function DocumentDetailTranslation({
  documentId,
  versionId,
  showOriginal,
  onToggleOriginal,
  onBeforeTranslate,
  translation,
}: {
  documentId: string;
  versionId: string | undefined;
  showOriginal: boolean;
  onToggleOriginal: () => void;
  onBeforeTranslate: () => void;
  translation: ReturnType<typeof useContentTranslation>;
}) {
  const tTrans = useTranslations('translations');
  if (translation.results.has(documentId)) {
    return <TranslatedBadge showOriginal={showOriginal} targetLanguage={translation.targetLanguage} onToggle={onToggleOriginal} />;
  }
  if (!translation.enabled || !versionId) return null;
  const pending = translation.pendingIds.has(documentId);
  const languageLabel = translation.targetLanguage === 'vi' ? tTrans('languageVi') : tTrans('languageEn');
  return (
    <div className="flex flex-wrap items-center gap-2">
      <TranslateActionButton
        label={tTrans('translateTo', { language: languageLabel })}
        pending={pending}
        onClick={() => {
          onBeforeTranslate();
          translation.translate([{ id: documentId, revision: versionId }]);
        }}
      />
      {!pending ? <TranslationNote issue={translation.issues.get(documentId)} /> : null}
    </div>
  );
}

/** Version preview body: loading, error retry, or bounded/translated content blockquote. */
function DocumentVersionPreview({
  isPending,
  isError,
  isSuccess,
  content,
  translatedExcerpt,
  onRetry,
}: {
  isPending: boolean;
  isError: boolean;
  isSuccess: boolean;
  content: string;
  translatedExcerpt: string | null | undefined;
  onRetry: () => void;
}) {
  const d = useTranslations('detail');
  if (isPending) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        {d('loading')}
      </p>
    );
  }
  if (isError) {
    return (
      <p className="error" role="alert">
        {d('contentUnavailable')}{' '}
        <Button type="button" variant="outline" size="sm" onClick={onRetry}>
          {d('retry')}
        </Button>
      </p>
    );
  }
  if (!isSuccess) return null;
  const shownContent = translatedExcerpt ?? content.slice(0, PREVIEW_LIMIT);
  const truncated = !translatedExcerpt && content.length > PREVIEW_LIMIT;
  return (
    <div className="grid gap-1">
      <h3 className="text-sm font-semibold">{d('contentPreview')}</h3>
      <blockquote className="whitespace-pre-wrap break-words border-l-2 border-border pl-3 text-sm leading-relaxed">
        {shownContent}
        {truncated ? '…' : ''}
      </blockquote>
    </div>
  );
}

/** Formats the observed-at and current-version subtitle line of a document detail dialog. */
function formatDocumentMeta(
  observedAt: string | null | undefined,
  currentVersion: number,
  locale: AppLocaleId,
  timezone: string,
  d: ReturnType<typeof useTranslations<'detail'>>,
): string {
  const observed = observedAt ? `${d('observedAt')} ${formatDateTime(observedAt, locale, timezone)}` : d('observedUnknown');
  return `${observed} · ${d('currentVersion', { version: currentVersion })} · ${timezone}`;
}

/** Read-only document summary for the shared detail dialog with on-demand translation; editing and deletion stay on the full document page. */
export function DocumentDialogBody({ documentId }: { documentId: string }) {
  const d = useTranslations('detail');
  const display = useDisplayPreferences();
  const [showOriginal, setShowOriginal] = useState(false);
  const document = useQuery({ queryKey: documentKeys.detail(documentId), queryFn: () => getDocument(documentId) });
  const version = useQuery({
    queryKey: [...documentKeys.versions(documentId), document.data?.current_version],
    queryFn: () => getVersion(documentId, document.data!.current_version),
    enabled: !!document.data,
  });
  const versionId = version.data?.id;
  const targets = useMemo(() => (versionId ? [{ id: documentId, revision: versionId }] : []), [documentId, versionId]);
  const translation = useContentTranslation('document', targets);
  const translated = showOriginal ? undefined : (translation.results.get(documentId)?.translation ?? undefined);

  if (document.isPending) {
    return (
      <p role="status" className="p-4 text-sm text-muted-foreground">
        {d('loading')}
      </p>
    );
  }
  if (document.isError || !document.data) {
    return (
      <p className="error p-4" role="alert">
        {d('documentUnavailable')}{' '}
        <Button type="button" variant="outline" size="sm" onClick={() => void document.refetch()}>
          {d('retry')}
        </Button>
      </p>
    );
  }
  const value = document.data;
  const externalUrl = safeHttpUrl(value.canonical_url);
  return (
    <section className="flex h-full min-h-0 flex-col gap-4 overflow-auto p-4" aria-labelledby="document-dialog-title">
      <header className="grid gap-1.5">
        <div className="flex items-start gap-2">
          <h2 id="document-dialog-title" className="flex-1 text-xl font-semibold leading-snug">
            {translated?.title ?? value.title}
          </h2>
          {externalUrl ? (
            <a
              href={externalUrl}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={d('open.original')}
              title={d('open.original')}
              className="inline-flex size-11 items-center justify-center rounded-[9px] text-muted-foreground hover:bg-secondary hover:text-foreground"
            >
              <ExternalLink aria-hidden="true" className="size-4" />
            </a>
          ) : null}
        </div>
        <p className="text-xs text-muted-foreground">
          {formatDocumentMeta(value.observed_at, value.current_version, display.locale, display.timezone, d)}
        </p>
        <DocumentDetailTranslation
          documentId={value.id}
          versionId={versionId}
          showOriginal={showOriginal}
          onToggleOriginal={() => setShowOriginal((v) => !v)}
          onBeforeTranslate={() => setShowOriginal(false)}
          translation={translation}
        />
      </header>
      <DocumentVersionPreview
        isPending={version.isPending}
        isError={version.isError}
        isSuccess={version.isSuccess}
        content={version.data?.content ?? ''}
        translatedExcerpt={translated?.excerpt}
        onRetry={() => void version.refetch()}
      />
      <footer className="mt-auto flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <Button asChild variant="outline">
          <Link href={`/knowledge/documents/${value.id}`}>{d('open.fullDocument')}</Link>
        </Button>
      </footer>
    </section>
  );
}
