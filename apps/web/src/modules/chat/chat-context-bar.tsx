'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { useQuery } from '@tanstack/react-query';
import { FileTextIcon, PlusIcon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import type { ChatContext } from '@/modules/chat/api';
import { searchDocuments, type SearchHit } from '@/modules/search/api';

/** Builds the human-readable label for the grounding context attached to the active chat. */
export function useContextLabel(context: ChatContext | null) {
  const t = useTranslations('chat');
  if (!context) return null;
  if (context.kind === 'day') return t('contextDay', { date: String(context.date ?? ''), timezone: String(context.timezone ?? '') });
  if (context.kind === 'document') return t('contextDocument', { title: String(context.resource_id ?? '') });
  if (context.kind === 'entity') return t('contextEntity', { name: String(context.resource_id ?? '') });
  return t('contextSelection', { count: context.items?.length ?? 1 });
}

/** Drops client-only chip titles from selection items; the server rejects unknown item fields. */
export function stripItemTitles(context: ChatContext | null): ChatContext | null {
  if (context?.kind !== 'selection' || !context.items) return context;
  return { ...context, items: context.items.map(({ sourceId, documentId, documentVersionId, chunkId }) => ({ sourceId, documentId, documentVersionId, chunkId })) };
}

/** One chunk-scoped ref per document keeps a 32-document selection inside the server's 100-ref limit. */
export const MAX_ITEMS = 32;
const noFilters = { source_ids: [], date_from: null, date_to: null, content_types: [] };
type Item = NonNullable<ChatContext['items']>[number];

/** Search-and-select dialog. Search does not filter local_only sources; the server re-fences every item at send, and local-only content is rejected later when the run uses a cloud model. */
function ContextPicker({ open, onOpenChange, current, onConfirm }: { open: boolean; onOpenChange: (open: boolean) => void; current: Item[]; onConfirm: (hits: SearchHit[]) => void }) {
  const t = useTranslations('chat');
  const [query, setQuery] = React.useState('');
  const [debounced, setDebounced] = React.useState('');
  const [picked, setPicked] = React.useState<Map<string, SearchHit>>(new Map());
  React.useEffect(() => {
    const id = setTimeout(() => setDebounced(query.trim()), 300);
    return () => clearTimeout(id);
  }, [query]);
  const results = useQuery({ queryKey: ['chat-context-picker', debounced], queryFn: () => searchDocuments(debounced, noFilters, 'hybrid'), enabled: open && debounced.length > 0, retry: false });
  const taken = new Set(current.map((i) => i.documentId));
  const room = MAX_ITEMS - current.length;
  const close = () => { setQuery(''); setDebounced(''); setPicked(new Map()); onOpenChange(false); };
  const toggle = (hit: SearchHit) => setPicked((prev) => {
    const next = new Map(prev);
    if (next.has(hit.document_id)) next.delete(hit.document_id);
    else if (next.size < room) next.set(hit.document_id, hit);
    return next;
  });
  const seen = new Set<string>();
  const hits = (results.data?.items ?? []).filter((h) => !seen.has(h.document_id) && !!seen.add(h.document_id));
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) close(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('contextPickerTitle')}</DialogTitle>
          <DialogDescription>{t('contextPickerHint', { max: MAX_ITEMS })}</DialogDescription>
        </DialogHeader>
        <Input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t('contextPickerSearch')} aria-label={t('contextPickerSearch')} autoFocus />
        <div role="group" aria-label={t('contextPickerResults')} className="flex max-h-72 flex-col gap-1 overflow-y-auto">
          {/* Always mounted so assistive tech announces the text change. */}
          <p role="status" className="text-sm text-muted-foreground">{(results.isFetching || (query.trim() !== debounced)) && query.trim() !== '' ? t('contextPickerSearching') : ''}</p>
          {results.isError && <p role="alert" className="text-sm text-destructive">{t('contextPickerError')}</p>}
          {results.isSuccess && !results.isFetching && hits.length === 0 && <p className="text-sm text-muted-foreground">{t('contextPickerEmpty')}</p>}
          {hits.map((hit) => {
            const already = taken.has(hit.document_id);
            const checked = already || picked.has(hit.document_id);
            return (
              <label key={hit.document_id} className="flex min-h-11 cursor-pointer items-start gap-2 rounded-md px-2 py-2 text-sm hover:bg-secondary">
                <Checkbox checked={checked} disabled={already || (!checked && picked.size >= room)} onCheckedChange={() => toggle(hit)} className="mt-0.5" />
                <span className="min-w-0"><span className="block truncate font-medium">{hit.title}</span><span className="block truncate text-xs text-muted-foreground">{hit.source.name}</span></span>
              </label>
            );
          })}
        </div>
        <p className="text-xs text-muted-foreground" role="status">{t('contextPickerCount', { count: current.length + picked.size, max: MAX_ITEMS })}</p>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={close}>{t('contextPickerCancel')}</Button>
          <Button type="button" disabled={picked.size === 0} onClick={() => { onConfirm([...picked.values()]); close(); }}>{t('contextPickerAdd')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Removable context chip row shared by the quick-chat drawer and the full Chat page; `onChange` enables the add-context picker. */
function focusChip(bar: HTMLElement | null, idx: number) {
  setTimeout(() => {
    const btns = bar?.querySelectorAll<HTMLButtonElement>('button[data-chip-remove]');
    (btns && btns.length ? btns[Math.min(idx, btns.length - 1)] : bar?.querySelector<HTMLButtonElement>('button[data-add-context]'))?.focus();
  }, 0);
}

export function ChatContextBar({ context, onRemove, onChange }: { context: ChatContext | null; onRemove: () => void; onChange?: (context: ChatContext | null) => void }) {
  const t = useTranslations('chat');
  const label = useContextLabel(context);
  const [open, setOpen] = React.useState(false);
  const barId = React.useId();
  const [announce, setAnnounce] = React.useState('');
  const canPick = !!onChange && (!context || context.kind === 'selection');
  if (!canPick && !label) return null;
  const items = context?.kind === 'selection' ? context.items ?? [] : [];
  const chip = (key: string, text: string, remove: () => void) => (
    <span key={key} className="inline-flex min-h-8 max-w-full items-center gap-1 rounded-full border border-border bg-background py-1 pl-2 pr-0">
      <FileTextIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="truncate">{text}</span>
      {/* 44px hit area; the negative margin keeps the chip's visual height. */}
      <Button type="button" variant="ghost" size="icon" className="-my-1.5 rounded-full text-muted-foreground" data-chip-remove onClick={remove} title={t('contextRemove', { label: text })} aria-label={t('contextRemove', { label: text })}>
        <XIcon className="size-3.5" aria-hidden="true" />
      </Button>
    </span>
  );
  const setItems = (next: Item[]) => {
    const unique = next.filter((i, k, a) => a.findIndex((x) => x.documentId === i.documentId) === k).slice(0, MAX_ITEMS);
    onChange?.(unique.length ? { kind: 'selection', items: unique } : null);
  };
  const nameOf = (i: Item, idx: number) => i.title ?? t('contextDocumentItem', { n: idx + 1 });
  const removeAt = (idx: number) => {
    const msg = t('contextRemoved', { label: nameOf(items[idx], idx) });
    setAnnounce('');
    setTimeout(() => setAnnounce(msg), 50);
    setItems(items.filter((_, k) => k !== idx));
    focusChip(document.getElementById(barId), idx);
  };
  return (
    <div id={barId} className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border bg-secondary px-4 py-2 text-xs text-foreground">
      {label && <span className="sr-only">{t('contextLabel')}</span>}
      {canPick && items.length > 0
        ? items.map((i, idx) => chip(i.documentId, nameOf(i, idx), () => removeAt(idx)))
        : label && chip('ctx', label, onRemove)}
      <span role="status" className="sr-only">{announce}</span>
      {canPick && (
        <Button type="button" variant="outline" size="sm" className="min-h-11" data-add-context disabled={items.length >= MAX_ITEMS} onClick={() => setOpen(true)}>
          <PlusIcon className="size-3.5" aria-hidden="true" />{t('addContext')}
        </Button>
      )}
      {canPick && (
        <ContextPicker open={open} onOpenChange={setOpen} current={items} onConfirm={(hits) => {
          setItems([...items, ...hits.map((h) => ({ sourceId: h.source.id, documentId: h.document_id, documentVersionId: h.document_version_id, chunkId: h.chunk_id, title: h.title }))]);
        }} />
      )}
    </div>
  );
}
