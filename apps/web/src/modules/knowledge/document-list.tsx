'use client';

import { useInfiniteQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { DocumentForm } from './document-form';
import { documentKeys, listDocuments } from './api';
import { Upload } from '@/modules/ingestion/upload';
import { useRealtime } from '@/core/realtime-provider';
import { useTranslations } from 'next-intl';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { useWorkspace } from '@/core/workspace-context';

/** Lists documents and consumes realtime refresh state for the knowledge document view. */
export function DocumentList() {
  const t = useTranslations('shell');
  const d = useTranslations('documents');
  const display = useDisplayPreferences();
  const realtime = useRealtime();
  const { isOwner } = useWorkspace();
  const [adding, setAdding] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [refreshingUpdates, setRefreshingUpdates] = useState(false);
  const documents = useInfiniteQuery({ queryKey: documentKeys.all, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listDocuments(pageParam), getNextPageParam: (last) => last.next_cursor ?? undefined });
  const items = documents.data?.pages.flatMap((page) => page.items) ?? [];
  /** Refreshes documents from realtime updates and clears the local busy indicator after the attempt settles. */
  const consumeUpdates = async () => {
    setRefreshingUpdates(true);
    try { await realtime.consumeDocumentUpdates(); } finally { setRefreshingUpdates(false); }
  };
  return <section className="rounded-xl border bg-card p-6"><div className="section-heading"><div><span className="brand">{d('brand')}</span><h1>{d('listTitle')}</h1><p className="text-muted-foreground">{d('listIntro')}</p></div><div className="form-actions">{(realtime.newDocumentCount > 0 || realtime.documentRefreshRequired) && <Button variant="outline" disabled={refreshingUpdates} onClick={() => void consumeUpdates()}>{refreshingUpdates ? t('reconnecting') : realtime.newDocumentCount > 0 ? t('newDocumentUpdates', { count: realtime.newDocumentCount }) : t('refreshDocumentList')}</Button>}{isOwner && <><Button variant="outline" onClick={() => setUploading((value) => !value)}>{d('uploadFile')}</Button><Button onClick={() => setAdding(true)}>{d('newDocument')}</Button></>}</div></div>
    {realtime.documentRefreshRequired && <p className="text-muted-foreground" role="status">{t('documentRefreshAvailable')}</p>}
    {realtime.documentRefreshFailed && <p className="error" role="alert">{t('documentRefreshFailed')}</p>}
    {adding && <DocumentForm onCancel={() => setAdding(false)} />}
    {uploading && <Upload />}
    {documents.isPending && <div className="skeleton" aria-label={d('loadingDocuments')} />}
    {documents.isError && <p className="error" role="alert">{d('listLoadFailed')} <Button variant="outline" onClick={() => documents.refetch()}>{d('retry')}</Button></p>}
    {documents.isSuccess && items.length === 0 && <p className="empty-state">{d('listEmpty')}</p>}
    {items.length > 0 && <ul className="record-list">{items.map((document) => <li key={document.id} className="record-row"><div className="min-w-0"><Link href={`/knowledge/documents/${document.id}`}><strong>{document.title}</strong></Link><p className="meta text-muted-foreground m-0">{d('versionUpdated', { version: document.current_version, updated: formatDateTime(document.updated_at, display.locale, display.timezone) })}</p></div><Badge variant="outline">v{document.current_version}</Badge></li>)}</ul>}
    {documents.hasNextPage && <Button variant="outline" disabled={documents.isFetchingNextPage} onClick={() => documents.fetchNextPage()}>{d('loadMore')}</Button>}
  </section>;
}
