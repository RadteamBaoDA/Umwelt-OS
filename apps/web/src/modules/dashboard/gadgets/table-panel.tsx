'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import React, { useMemo, useState } from 'react';
import { Button } from '@/components/ui/button';
import { useChatController } from '@/core/app-shell/chat-controller';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { documentKeys, listGadgetDocumentProjections } from '@/modules/knowledge/api';
import type { GadgetInstance } from '../api';

/** Props for the source-backed table gadget. */
export interface TablePanelProps {
  /** Saved gadget instance and source scope. */
  instance: GadgetInstance;
}

type SelectedRecordIdentity = {
  sourceId: string;
  documentId: string;
  documentVersionId: string;
  versionNumber: number;
};

/** Render current source records and open Ask AI with exact selected versions. */
export function TablePanel({ instance }: TablePanelProps) {
  const t = useTranslations('dashboard');
  const { openDrawer } = useChatController();
  const display = useDisplayPreferences();
  const sourceIds = [...new Set(instance.definition.source_ids)].slice(0, 32);
  const [selectedRecords, setSelectedRecords] = useState<Record<string, SelectedRecordIdentity>>({});
  const query = useQuery({
    queryKey: [...documentKeys.all, 'table-panel', sourceIds],
    enabled: sourceIds.length > 0,
    queryFn: () => listGadgetDocumentProjections(sourceIds),
  });
  const records = useMemo(() => query.data?.items ?? [], [query.data]);
  const selected = Object.values(selectedRecords);
  const selectedIds = new Set(Object.keys(selectedRecords));
  const currentRecords = new Map(records.map((record) => [record.document_id, record]));
  const staleSelections = selected.filter((item) =>
    currentRecords.get(item.documentId)?.document_version_id !== item.documentVersionId,
  );

  return (
    <section className="flex h-full flex-col gap-3 overflow-hidden bg-card p-3 text-card-foreground">
      <header className="flex items-center justify-between border-b border-border pb-2">
        <h2 className="text-xs font-semibold">{t('tablePanelTitle')}</h2>
        <Button
          type="button"
          size="sm"
          disabled={selected.length === 0}
          onClick={() => openDrawer({ context: {
            kind: 'selection',
            items: selected.map(({ sourceId, documentId, documentVersionId }) => ({
              sourceId, documentId, documentVersionId,
            })),
          } })}
        >
          {t('askAboutSelected', { count: selected.length })}
        </Button>
      </header>
      {staleSelections.length > 0 && (
        <p role="status" className="text-xs text-muted-foreground">{t('selectionChanged', { count: staleSelections.length })}</p>
      )}
      {sourceIds.length === 0 ? <p role="status" className="text-sm text-muted-foreground">{t('gadgetDataEmpty')}</p>
        : query.isPending ? <p role="status" className="text-sm text-muted-foreground">{t('gadgetDataLoading')}</p>
          : query.isError ? <p role="alert" className="text-sm text-destructive">{t('gadgetDataLoadFailed')}</p>
            : records.length === 0 ? <p role="status" className="text-sm text-muted-foreground">{t('gadgetDataEmpty')}</p>
              : (
                <div className="min-h-0 flex-1 overflow-auto">
                  <table className="w-full text-left text-xs">
                    <thead><tr className="border-b border-border text-muted-foreground">
                      <th scope="col" className="p-2">{t('selectRow')}</th>
                      <th scope="col" className="p-2">{t('recordTitle')}</th>
                      <th scope="col" className="p-2">{t('recordDate')}</th>
                    </tr></thead>
                    <tbody>
                      {records.map((record) => (
                        <tr key={record.document_id} className="border-b border-border/60 align-top">
                          <td className="p-2">
                            <input
                              type="checkbox"
                              aria-label={t('selectRecord', { title: record.title })}
                              checked={selectedIds.has(record.document_id)}
                              onChange={(event) => setSelectedRecords((current) => {
                                if (!event.target.checked) {
                                  const next = { ...current };
                                  delete next[record.document_id];
                                  return next;
                                }
                                if (Object.keys(current).length >= 32) return current;
                                return { ...current, [record.document_id]: {
                                  sourceId: record.source_id,
                                  documentId: record.document_id,
                                  documentVersionId: record.document_version_id,
                                  versionNumber: record.version_number,
                                } };
                              })}
                            />
                          </td>
                          <td className="p-2">
                            <p className="font-medium">{record.title}</p>
                            <p className="mt-1 line-clamp-2 text-muted-foreground">{record.excerpt}</p>
                          </td>
                          <td className="whitespace-nowrap p-2 text-muted-foreground">{formatDateTime(record.published_at ?? record.observed_at, display.locale, display.timezone)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
    </section>
  );
}
