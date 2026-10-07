'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { FileTextIcon, LockIcon, PaperclipIcon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
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
  /** Kept so a private attachment can be re-attached as shared without picking it again. */
  file: File;
  doc?: ChatAttachment;
  error?: string;
}

/** Machine code of the server's 409 for a selection that includes local-only content. */
export const SELECTION_LOCAL_ONLY = 'selection_local_only';
/** Element id of the attachment status line; the composer's Send button references it when blocked. */
export const ATTACHMENT_STATUS_ID = 'chat-attachment-status';
const POLL_MS = 1500;
/** Consecutive non-404 polling errors tolerated (with exponential backoff) before giving up. */
const MAX_POLL_FAILURES = 5;
const MAX_SIZE_LABEL = `${ATTACHMENT_MAX_BYTES / (1024 * 1024)} MiB`;
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
    while (!signal.aborted) {
      await new Promise((resolve) => setTimeout(resolve, POLL_MS * 2 ** failures));
      if (signal.aborted) return;
      try {
        const doc = await getChatAttachment(documentId, signal);
        failures = 0;
        if (doc.status !== 'pending') { patch(key, { doc, state: doc.status }); return; }
      } catch (err: unknown) {
        if (signal.aborted) return;
        if ((err instanceof ApiError && err.status === 404) || ++failures > MAX_POLL_FAILURES) {
          patch(key, { state: 'failed' });
          return;
        }
      }
    }
  }, [patch]);

  const upload = React.useCallback((key: string, file: File, share: boolean) => {
    const controller = new AbortController();
    controllers.current.set(key, controller);
    uploadChatAttachment(file, csrfToken, share, controller.signal)
      .then((doc) => {
        patch(key, { doc, state: doc.status });
        if (doc.status === 'pending') void poll(key, doc.document_id, controller.signal);
      })
      .catch((err: unknown) => { if (!controller.signal.aborted) patch(key, { state: 'rejected', error: t(uploadErrorKey(err), { size: MAX_SIZE_LABEL }) }); });
  }, [csrfToken, patch, poll, t]);

  /** Validates type and size locally (the server re-validates), then uploads each file that fits. */
  const add = React.useCallback((files: File[], share = shareWithModel) => {
    const room = Math.max(0, MAX_ATTACHMENTS - entriesRef.current.length);
    setDropped(Math.max(0, files.length - room));
    const added = files.slice(0, room).map((file): AttachmentEntry => {
      const ext = file.name.includes('.') ? `.${file.name.split('.').pop()!.toLowerCase()}` : '';
      const error = !allowed.has(ext) ? t('attachmentType') : file.size > ATTACHMENT_MAX_BYTES ? t('attachmentTooBig', { size: MAX_SIZE_LABEL }) : undefined;
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

/** Attach button, cloud-sharing opt-in and status chips for the full Chat composer. */
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
  const note = state.dropped > 0
    ? t('attachmentDropped', { max: MAX_ATTACHMENTS })
    : state.entries.length ? t('attachmentNote', { max: MAX_ATTACHMENTS }) : '';
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
        <div className="flex items-center gap-2">
          <Checkbox
            id="chat-attachment-share"
            checked={state.shareWithModel}
            disabled={disabled}
            onCheckedChange={(checked) => state.setShareWithModel(checked === true)}
            aria-describedby="chat-attachment-share-help"
          />
          <Label htmlFor="chat-attachment-share" className="text-xs text-foreground">{t('attachmentShareToggle')}</Label>
        </div>
        {state.entries.map((e) => {
          const problem = e.state === 'rejected' || e.state === 'failed' || e.state === 'too_large';
          return (
            <span key={e.key} className={`inline-flex min-h-8 max-w-full items-center gap-1.5 rounded-full border bg-background py-0.5 pl-2.5 pr-0 text-xs ${problem ? 'border-destructive' : 'border-border'}`}>
              {e.doc?.local_only && e.state === 'ready'
                ? <LockIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />
                : <FileTextIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />}
              <span className="truncate text-foreground">{e.name}</span>
              <span className={problem ? 'text-destructive' : 'text-muted-foreground'}>· {statusText(e)}</span>
              {/* 44px hit area; the negative margin keeps the chip's visual height. */}
              <Button type="button" variant="ghost" size="icon" className="-my-1.5 rounded-full text-muted-foreground" onClick={() => state.remove(e.key)} title={t('attachmentRemove', { name: e.name })} aria-label={t('attachmentRemove', { name: e.name })}>
                <XIcon className="size-3.5" aria-hidden="true" />
              </Button>
            </span>
          );
        })}
      </div>
      <p id="chat-attachment-share-help" className="text-[11px] text-muted-foreground">{t('attachmentShareHelp')}</p>
      <div className="flex flex-wrap items-center gap-2">
        <p id={ATTACHMENT_STATUS_ID} role="status" aria-live="polite" className="text-[11px] text-muted-foreground">
          {state.sendBlock ?? note}
        </p>
        {state.showReattach && (
          <Button type="button" variant="outline" size="sm" disabled={disabled} onClick={state.reattachShared}>
            {t('attachmentReattachShared')}
          </Button>
        )}
      </div>
    </div>
  );
}
