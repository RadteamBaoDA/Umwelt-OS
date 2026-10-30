'use client';

import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { getDocument, getVersion } from '@/modules/knowledge/api';

export interface ChatDocumentPreviewProps {
  /** Document id admitted by the existing search or chat-attachment flow. */
  documentId: string | null;
  /** Selected immutable version id; content is hidden if it no longer matches the current version. */
  versionId?: string | null;
  /** Plain text title shown by the dialog. */
  title: string;
  /** Accessible button that opens the read-only text preview. */
  children: React.ReactElement;
  /** Status to show when upload or ingestion has not produced a readable version. */
  unavailableMessage?: string;
}

/**
 * Opens an owner-authorized document version in a text-only dialog. The query runs only while open;
 * content is shown only when the selected version is still current, avoiding a misleading preview
 * after the chat context has become stale.
 *
 * @param props - Document identity, selected version, trigger button, and optional pending status.
 * @returns Accessible dialog trigger and plaintext document preview.
 */
export function ChatDocumentPreview({ documentId, versionId, title, children, unavailableMessage }: ChatDocumentPreviewProps) {
  const t = useTranslations('chat');
  const [open, setOpen] = React.useState(false);
  const preview = useQuery({
    queryKey: ['chat-document-preview', documentId, versionId],
    queryFn: async () => {
      if (!documentId) return null;
      const document = await getDocument(documentId);
      const version = await getVersion(documentId, document.current_version);
      return { content: version.content, matches: !versionId || version.id === versionId };
    },
    enabled: open && !!documentId,
    retry: false,
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent className="max-h-[80dvh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="break-words">{title}</DialogTitle>
          <DialogDescription>{t('previewTextOnly')}</DialogDescription>
        </DialogHeader>
        {!documentId && <p className="text-sm text-muted-foreground">{unavailableMessage ?? t('previewUnavailable')}</p>}
        {documentId && preview.isFetching && <p role="status" className="text-sm text-muted-foreground">{t('previewLoading')}</p>}
        {documentId && preview.isError && <p role="alert" className="text-sm text-destructive">{t('previewUnavailable')}</p>}
        {documentId && preview.isSuccess && !preview.isFetching && !preview.data?.matches && <p className="text-sm text-muted-foreground">{t('previewVersionChanged')}</p>}
        {documentId && preview.isSuccess && !preview.isFetching && preview.data?.matches && (
          <pre className="max-h-[60dvh] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-background p-3 text-sm text-foreground">{preview.data.content}</pre>
        )}
      </DialogContent>
    </Dialog>
  );
}
