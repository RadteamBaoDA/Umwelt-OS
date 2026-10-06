'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { ExternalLink } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/core/i18n';
import { safeHttpUrl } from '@/core/safe-url';
import { useDisplayPreferences } from '@/core/query-provider';
import { documentKeys, getDocument, getVersion } from '@/modules/knowledge/api';

const PREVIEW_LIMIT = 2000;

/** Read-only document summary for the shared detail dialog; editing and deletion stay on the full document page. */
export function DocumentDialogBody({ documentId }: { documentId: string }) {
  const d = useTranslations('detail');
  const display = useDisplayPreferences();
  const document = useQuery({ queryKey: documentKeys.detail(documentId), queryFn: () => getDocument(documentId) });
  const version = useQuery({
    queryKey: [...documentKeys.versions(documentId), document.data?.current_version],
    queryFn: () => getVersion(documentId, document.data!.current_version),
    enabled: !!document.data,
  });
  if (document.isPending) return <p role="status" className="p-4 text-sm text-muted-foreground">{d('loading')}</p>;
  if (document.isError || !document.data) return <p className="error p-4" role="alert">{d('documentUnavailable')} <Button type="button" variant="outline" size="sm" onClick={() => { void document.refetch(); }}>{d('retry')}</Button></p>;
  const value = document.data;
  const content = version.data?.content ?? '';
  return <section className="flex h-full min-h-0 flex-col gap-4 overflow-auto p-4" aria-labelledby="document-dialog-title">
    <header className="grid gap-1">
      <div className="flex items-start gap-2">
        <h2 id="document-dialog-title" className="flex-1 text-xl font-semibold leading-snug">{value.title}</h2>
        {safeHttpUrl(value.canonical_url) ? <a href={safeHttpUrl(value.canonical_url)} target="_blank" rel="noopener noreferrer" aria-label={d('openOriginal')} title={d('openOriginal')} className="inline-flex size-11 items-center justify-center rounded-[9px] text-muted-foreground hover:bg-secondary hover:text-foreground"><ExternalLink aria-hidden="true" className="size-4" /></a> : null}
      </div>
      <p className="text-xs text-muted-foreground">
        {value.observed_at ? `${d('observedAt')} ${formatDateTime(value.observed_at, display.locale, display.timezone)}` : d('observedUnknown')} · {d('currentVersion', { version: value.current_version })} · {display.timezone}
      </p>
    </header>
    {version.isPending && <p role="status" className="text-sm text-muted-foreground">{d('loading')}</p>}
    {version.isError && <p className="error" role="alert">{d('contentUnavailable')} <Button type="button" variant="outline" size="sm" onClick={() => { void version.refetch(); }}>{d('retry')}</Button></p>}
    {version.isSuccess && <div className="grid gap-1">
      <h3 className="text-sm font-semibold">{d('contentPreview')}</h3>
      <blockquote className="whitespace-pre-wrap break-words border-l-2 border-border pl-3 text-sm leading-relaxed">{content.slice(0, PREVIEW_LIMIT)}{content.length > PREVIEW_LIMIT ? '…' : ''}</blockquote>
    </div>}
    <footer className="mt-auto flex flex-wrap items-center gap-2 border-t border-border pt-3">
      <Button asChild variant="outline"><Link href={`/knowledge/documents/${value.id}`}>{d('openFullDocument')}</Link></Button>
    </footer>
  </section>;
}
