'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { FileTextIcon, LockIcon, XIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
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
import { ChatDocumentPreview } from './chat-document-preview';

type Item = NonNullable<ChatContext['items']>[number];

/** One composer attachment: local upload state plus the server Document once accepted. */
export interface AttachmentEntry {
  key: string;
  name: string;
  state: 'uploading' | ChatAttachment['status'] | 'rejected';
  /** Kept so a private attachment can be re-attached as shared without picking it again. */
  file: File;
  doc?: ChatAttachment;
  error?: string;
}

/** Machine code of the server's 409 for a selection that includes local-only content. */
export const SELECTION_LOCAL_ONLY = 'selection_local_only';
/** Element id of the attachment status line; the composer's Send button references it when blocked. */
export const ATTACHMENT_STATUS_ID = 'chat-attachment-status';
/** Element id of the composer's + menu trigger; focus lands here when the last attachment chip is removed. */
export const COMPOSER_MENU_ID = 'chat-composer-menu';
const POLL_MS = 1500;
/** Consecutive non-404 polling errors tolerated (with exponential backoff) before giving up. */
const MAX_POLL_FAILURES = 5;
/** Overall cap on one attachment's ingest polling, whatever the server keeps reporting. */
const MAX_POLL_MS = 10 * 60_000;
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
 * Owns the full Chat composer's attachments. Each file is uploaded to a server-chosen source (the
 * private, local-only "Chat attachments" by default; "Chat attachments (shared)" when the owner opts
 * in) and polled until ingest finishes. Only `ready` attachments become `selection` items; `sendBlock`
 * explains why a send must wait or cannot include them. Each entry owns an AbortController that
 * cancels its upload and polling when it is removed.
 */
export function useChatAttachments(csrfToken: string, unavailable = false) {
  const t = useTranslations('chat');
  const [entries, setEntries] = React.useState<AttachmentEntry[]>([]);
  const [shareWithModel, setShareWithModel] = React.useState(false);
  const [dropped, setDropped] = React.useState(0);
  // Synchronous mirror of `entries`, so the remaining room is computed from the latest state, never a stale render.
  const entriesRef = React.useRef<AttachmentEntry[]>([]);
  const controllers = React.useRef(new Map<string, AbortController>());

  const update = React.useCallback((fn: (prev: AttachmentEntry[]) => AttachmentEntry[]) => {
    entriesRef.current = fn(entriesRef.current);
    setEntries(entriesRef.current);
  }, []);
  const patch = React.useCallback((key: string, next: Partial<AttachmentEntry>) => {
    update((prev) => prev.map((e) => (e.key === key ? { ...e, ...next } : e)));
  }, [update]);

  /** Polls one accepted Document until ingest settles; only a 404 fails it, other errors back off within a bound. */
  const poll = React.useCallback(async (key: string, documentId: string, signal: AbortSignal) => {
    let failures = 0;
    const deadline = Date.now() + MAX_POLL_MS;
    while (!signal.aborted) {
      await new Promise((resolve) => setTimeout(resolve, POLL_MS * 2 ** failures));
      if (signal.aborted) return;
      try {
        const doc = await getChatAttachment(documentId, signal);
        failures = 0;
        if (doc.status !== 'pending') { patch(key, { doc, state: doc.status }); return; }
        if (Date.now() > deadline) { patch(key, { state: 'failed', error: t('attachmentTimedOut') }); return; }
      } catch (err: unknown) {
        if (signal.aborted) return;
        if ((err instanceof ApiError && err.status === 404) || ++failures > MAX_POLL_FAILURES) {
          patch(key, { state: 'failed' });
          return;
        }
      }
    }
  }, [patch, t]);

  const upload = React.useCallback((key: string, file: File, share: boolean) => {
    const controller = new AbortController();
    controllers.current.set(key, controller);
    uploadChatAttachment(file, csrfToken, share, controller.signal)
      .then((doc) => {
        patch(key, { doc, state: doc.status });
        if (doc.status === 'pending') void poll(key, doc.document_id, controller.signal);
      })
      .catch((err: unknown) => { if (!controller.signal.aborted) patch(key, { state: 'rejected', error: t(uploadErrorKey(err)) }); });
  }, [csrfToken, patch, poll, t]);

  /** Validates type and size locally (the server re-validates), then uploads each file that fits. */
  const add = React.useCallback((files: File[], share = shareWithModel) => {
    const room = Math.max(0, MAX_ATTACHMENTS - entriesRef.current.length);
    setDropped(Math.max(0, files.length - room));
    const added = files.slice(0, room).map((file): AttachmentEntry => {
      const ext = file.name.includes('.') ? `.${file.name.split('.').pop()!.toLowerCase()}` : '';
      const error = !allowed.has(ext) ? t('attachmentType') : file.size > ATTACHMENT_MAX_BYTES ? t('attachmentTooBig') : undefined;
      return { key: `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`, name: file.name, file, state: error ? 'rejected' : 'uploading', error };
    });
    update((prev) => [...prev, ...added]);
    for (const e of added) if (e.state === 'uploading') upload(e.key, e.file, share);
  }, [shareWithModel, t, update, upload]);

  const remove = React.useCallback((key: string) => {
    controllers.current.get(key)?.abort();
    controllers.current.delete(key);
    setDropped(0);
    update((prev) => prev.filter((e) => e.key !== key));
  }, [update]);

  const clear = React.useCallback(() => {
    for (const c of controllers.current.values()) c.abort();
    controllers.current.clear();
    setDropped(0);
    setShareWithModel(false);
    update(() => []);
  }, [update]);

  /** Replaces every private (local-only) attachment with a fresh upload to the shared source. */
  const reattachShared = React.useCallback(() => {
    const privateEntries = entriesRef.current.filter((e) => e.doc?.local_only);
    for (const e of privateEntries) remove(e.key);
    add(privateEntries.map((e) => e.file), true);
  }, [add, remove]);

  // Abort in-flight uploads and polling when the composer unmounts.
  React.useEffect(() => {
    const live = controllers.current;
    return () => { for (const c of live.values()) c.abort(); };
  }, []);

  const items: Item[] = entries
    .filter((e) => e.state === 'ready' && e.doc?.document_version_id)
    .map((e) => ({ sourceId: e.doc!.source_id, documentId: e.doc!.document_id, documentVersionId: e.doc!.document_version_id! }));

  let sendBlock: string | null = null;
  let showReattach = false;
  if (unavailable && entries.length) sendBlock = t('attachmentUnavailableBlock');
  else if (entries.some((e) => e.state === 'uploading' || e.state === 'pending')) sendBlock = t('attachmentPendingBlock');
  else if (entries.some((e) => e.state === 'failed' || e.state === 'too_large' || e.state === 'rejected')) sendBlock = t('attachmentFailedBlock');
  else if (entries.some((e) => e.doc?.local_only)) { sendBlock = t('attachmentLocalOnlyBlock'); showReattach = true; }

  return {
    entries, items, add, remove, clear, reattachShared, sendBlock, dropped, showReattach,
    shareWithModel, setShareWithModel,
    full: entries.length >= MAX_ATTACHMENTS,
  };
}

/** Merges ready attachment refs into the chat context as `selection` items (deduplicated by document). */
export function withAttachments(context: ChatContext | null, items: Item[]): ChatContext | null {
  if (!items.length) return context;
  const base = context?.kind === 'selection' ? context.items ?? [] : [];
  const merged = [...base, ...items].filter((i, k, a) => a.findIndex((x) => x.documentId === i.documentId) === k);
  return { kind: 'selection', items: merged };
}

/**
 * Hidden file input, attachment text-preview badges and the live status line for the full Chat composer. The Attach
 * and cloud-sharing controls live in the composer's + menu, which opens the picker through `inputRef`.
 *
 * @param state - Attachment state from `useChatAttachments`.
 * @param disabled - Disables chip removal side effects such as the re-attach button.
 * @param inputRef - Ref to the hidden file input, shared with the + menu's Attach item.
 * @returns The chips row (only when entries exist) and the status line (only when it has text).
 */
export function ChatAttachmentBar({ state, disabled, inputRef }: { state: ReturnType<typeof useChatAttachments>; disabled?: boolean; inputRef: React.RefObject<HTMLInputElement | null> }) {
  const t = useTranslations('chat');
  const rootRef = React.useRef<HTMLDivElement>(null);
  const [removedNote, setRemovedNote] = React.useState('');
  /** Removes an entry, announces it, and moves focus to a neighbouring remove button (or the + menu) so it never falls to <body>. */
  const removeEntry = (key: string, name: string, idx: number) => {
    state.remove(key);
    setRemovedNote(t('attachmentRemoved', { name }));
    setTimeout(() => {
      const btns = rootRef.current?.querySelectorAll<HTMLButtonElement>('button[data-attachment-remove]');
      (btns && btns.length ? btns[Math.min(idx, btns.length - 1)] : document.getElementById(COMPOSER_MENU_ID))?.focus();
    }, 0);
  };
  const statusText = (e: AttachmentEntry) => {
    if (e.state === 'rejected') return e.error ?? t('attachmentUploadFailed');
    if (e.state === 'uploading') return t('attachmentUploading');
    if (e.state === 'pending') return t('attachmentProcessing');
    if (e.state === 'ready') return e.doc?.local_only ? t('attachmentLocalOnly') : t('attachmentReady');
    if (e.state === 'too_large') return t('attachmentTooLargeContext');
    return e.error ?? t('attachmentFailed');
  };
  const note = state.dropped > 0
    ? t('attachmentDropped', { max: MAX_ATTACHMENTS })
    : state.entries.length ? t('attachmentNote', { max: MAX_ATTACHMENTS }) : '';
  const statusLine = state.sendBlock ?? note;
  return (
    <div ref={rootRef} className="contents">
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
      {state.entries.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {state.entries.map((e, idx) => {
            const problem = e.state === 'rejected' || e.state === 'failed' || e.state === 'too_large';
            return (
              <Badge key={e.key} variant="secondary" className={`min-h-8 max-w-full gap-1 py-1 pl-1 pr-0 text-xs ${problem ? 'border border-destructive' : ''}`}>
                <ChatDocumentPreview
                  documentId={e.doc?.document_version_id && (e.state === 'ready' || e.state === 'too_large') ? e.doc.document_id : null}
                  versionId={e.state === 'ready' || e.state === 'too_large' ? e.doc?.document_version_id : undefined}
                  title={e.name}
                  unavailableMessage={e.state === 'uploading' || e.state === 'pending' ? t('attachmentPreviewWaiting') : t('previewUnavailable')}
                >
                  <Button type="button" variant="ghost" className="h-7 min-w-0 gap-1 px-1 text-xs font-normal" aria-label={t('previewDocument', { title: e.name })}>
                    {e.doc?.local_only && e.state === 'ready'
                      ? <LockIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />
                      : <FileTextIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />}
                    <span className="max-w-48 truncate">{e.name}</span>
                  </Button>
                </ChatDocumentPreview>
                <span className={problem ? 'text-destructive' : 'text-muted-foreground'}>· {statusText(e)}</span>
                {/* 44px hit area; the negative margin keeps the chip's visual height. */}
                <Button type="button" variant="ghost" size="icon" className="-my-1.5 rounded-full text-muted-foreground" data-attachment-remove onClick={() => removeEntry(e.key, e.name, idx)} title={t('attachmentRemove', { name: e.name })} aria-label={t('attachmentRemove', { name: e.name })}>
                  <XIcon className="size-3.5" aria-hidden="true" />
                </Button>
              </Badge>
            );
          })}
        </div>
      )}
      <span role="status" className="sr-only">{removedNote}</span>
      {/* Rendered only with text; Send's aria-describedby points here only while a block is active, so the id exists then. */}
      {(statusLine || state.showReattach) && (
        <div className="flex flex-wrap items-center gap-2">
          {statusLine && (
            <p id={ATTACHMENT_STATUS_ID} role="status" aria-live="polite" className="text-xs text-muted-foreground">{statusLine}</p>
          )}
          {state.showReattach && (
            <Button type="button" variant="outline" size="sm" disabled={disabled} onClick={state.reattachShared}>
              {t('attachmentReattachShared')}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
