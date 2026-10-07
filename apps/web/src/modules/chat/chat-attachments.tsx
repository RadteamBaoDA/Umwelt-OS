'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { FileTextIcon, LockIcon, PaperclipIcon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/core/api';
import {
  ATTACHMENT_ACCEPT,
  ATTACHMENT_MAX_BYTES,
  MAX_ATTACHMENTS,
  getChatAttachment,
  uploadChatAttachment,
  type ChatAttachment,
  type ChatContext,
} from '@/modules/chat/api';

type Item = NonNullable<ChatContext['items']>[number];

/** One composer attachment: local upload state plus the server Document once accepted. */
export interface AttachmentEntry {
  key: string;
  name: string;
  state: 'uploading' | ChatAttachment['status'] | 'rejected';
  doc?: ChatAttachment;
  error?: string;
}

const POLL_MS = 1500;
/** Element id of the attachment status line; the composer's Send button references it when blocked. */
export const ATTACHMENT_STATUS_ID = 'chat-attachment-status';
const allowed = new Set(ATTACHMENT_ACCEPT.split(','));

/** Maps an upload failure to localized copy; the server's English detail is never shown verbatim. */
function uploadErrorKey(err: unknown): 'attachmentTooBig' | 'attachmentType' | 'attachmentInactive' | 'attachmentUploadFailed' {
  if (err instanceof ApiError) {
    if (err.status === 413) return 'attachmentTooBig';
    if (err.status === 415) return 'attachmentType';
    if (err.status === 409) return 'attachmentInactive';
  }
  return 'attachmentUploadFailed';
}

/**
 * Owns the full Chat composer's attachments. Each file is uploaded to the server-chosen,
 * local-only "Chat attachments" source and polled until ingest finishes. Only `ready`
 * attachments become `selection` items; `sendBlock` explains why a send must wait or cannot
 * include them (a send path that carries no context, pending ingest, failed files, or local-only
 * content the cloud model may not receive).
 */
export function useChatAttachments(csrfToken: string, unavailable = false) {
  const t = useTranslations('chat');
  const [entries, setEntries] = React.useState<AttachmentEntry[]>([]);

  const patch = React.useCallback((key: string, next: Partial<AttachmentEntry>) => {
    setEntries((prev) => prev.map((e) => (e.key === key ? { ...e, ...next } : e)));
  }, []);

  /** Validates type and size locally (the server re-validates), then uploads each file. */
  const add = React.useCallback((files: File[]) => {
    const room = MAX_ATTACHMENTS - entries.length;
    for (const file of files.slice(0, Math.max(0, room))) {
      const key = `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
      const ext = file.name.includes('.') ? `.${file.name.split('.').pop()!.toLowerCase()}` : '';
      const localError = !allowed.has(ext) ? t('attachmentType') : file.size > ATTACHMENT_MAX_BYTES ? t('attachmentTooBig') : null;
      setEntries((prev) => [...prev, { key, name: file.name, state: localError ? 'rejected' : 'uploading', error: localError ?? undefined }]);
      if (localError) continue;
      uploadChatAttachment(file, csrfToken)
        .then((doc) => patch(key, { doc, state: doc.status }))
        .catch((err: unknown) => patch(key, { state: 'rejected', error: t(uploadErrorKey(err)) }));
    }
  }, [csrfToken, entries.length, patch, t]);

  const remove = React.useCallback((key: string) => setEntries((prev) => prev.filter((e) => e.key !== key)), []);
  const clear = React.useCallback(() => setEntries([]), []);

  // Poll ingest status while any accepted attachment is still parsing.
  const pendingIds = entries.filter((e) => e.state === 'pending' && e.doc).map((e) => `${e.key}|${e.doc!.document_id}`).join(',');
  React.useEffect(() => {
    if (!pendingIds) return;
    const id = setInterval(() => {
      for (const pair of pendingIds.split(',')) {
        const [key, documentId] = pair.split('|');
        getChatAttachment(documentId)
          .then((doc) => { if (doc.status !== 'pending') patch(key, { doc, state: doc.status }); })
          .catch(() => patch(key, { state: 'failed' }));
      }
    }, POLL_MS);
    return () => clearInterval(id);
  }, [pendingIds, patch]);

  const items: Item[] = entries
    .filter((e) => e.state === 'ready' && e.doc?.document_version_id)
    .map((e) => ({ sourceId: e.doc!.source_id, documentId: e.doc!.document_id, documentVersionId: e.doc!.document_version_id! }));

  let sendBlock: string | null = null;
  if (unavailable && entries.length) sendBlock = t('attachmentUnavailableBlock');
  else if (entries.some((e) => e.state === 'uploading' || e.state === 'pending')) sendBlock = t('attachmentPendingBlock');
  else if (entries.some((e) => e.state === 'failed' || e.state === 'too_large' || e.state === 'rejected')) sendBlock = t('attachmentFailedBlock');
  else if (entries.some((e) => e.doc?.local_only)) sendBlock = t('attachmentLocalOnlyBlock');

  return { entries, items, add, remove, clear, sendBlock, full: entries.length >= MAX_ATTACHMENTS };
}

/** Merges ready attachment refs into the chat context as `selection` items (deduplicated by document). */
export function withAttachments(context: ChatContext | null, items: Item[]): ChatContext | null {
  if (!items.length) return context;
  const base = context?.kind === 'selection' ? context.items ?? [] : [];
  const merged = [...base, ...items].filter((i, k, a) => a.findIndex((x) => x.documentId === i.documentId) === k);
  return { kind: 'selection', items: merged };
}

/** Attach button plus status chips for the full Chat composer. */
export function ChatAttachmentBar({ state, disabled }: { state: ReturnType<typeof useChatAttachments>; disabled?: boolean }) {
  const t = useTranslations('chat');
  const inputRef = React.useRef<HTMLInputElement>(null);
  const statusText = (e: AttachmentEntry) => {
    if (e.state === 'rejected') return e.error ?? t('attachmentUploadFailed');
    if (e.state === 'uploading') return t('attachmentUploading');
    if (e.state === 'pending') return t('attachmentProcessing');
    if (e.state === 'ready') return e.doc?.local_only ? t('attachmentLocalOnly') : t('attachmentReady');
    if (e.state === 'too_large') return t('attachmentTooLargeContext');
    return t('attachmentFailed');
  };
  return (
    <div className="flex flex-col gap-1.5 px-1">
      <div className="flex flex-wrap items-center gap-2">
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ATTACHMENT_ACCEPT}
          className="sr-only"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(e) => { state.add(Array.from(e.target.files ?? [])); e.target.value = ''; }}
        />
        <Button type="button" variant="outline" size="sm" className="min-h-11" disabled={disabled || state.full} onClick={() => inputRef.current?.click()}>
          <PaperclipIcon className="size-3.5" aria-hidden="true" />{t('attach')}
        </Button>
        {state.entries.map((e) => {
          const problem = e.state === 'rejected' || e.state === 'failed' || e.state === 'too_large';
          return (
            <span key={e.key} className={`inline-flex min-h-8 max-w-full items-center gap-1.5 rounded-full border bg-background py-0.5 pl-2.5 pr-1 text-xs ${problem ? 'border-destructive' : 'border-border'}`}>
              {e.doc?.local_only && e.state === 'ready'
                ? <LockIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />
                : <FileTextIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />}
              <span className="truncate text-foreground">{e.name}</span>
              <span className={problem ? 'text-destructive' : 'text-muted-foreground'}>· {statusText(e)}</span>
              <button type="button" onClick={() => state.remove(e.key)} className="inline-flex size-6 items-center justify-center rounded-full text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" title={t('attachmentRemove', { name: e.name })} aria-label={t('attachmentRemove', { name: e.name })}>
                <XIcon className="size-3.5" aria-hidden="true" />
              </button>
            </span>
          );
        })}
      </div>
      <p id={ATTACHMENT_STATUS_ID} role="status" aria-live="polite" className="text-[11px] text-muted-foreground">
        {state.sendBlock ?? (state.entries.length ? t('attachmentNote', { max: MAX_ATTACHMENTS }) : '')}
      </p>
    </div>
  );
}
