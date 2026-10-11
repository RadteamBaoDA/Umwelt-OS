'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { useQuery } from '@tanstack/react-query';
import { SearchIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { commandDestinations, destinationEnabled, destinationForRole, type ModuleAvailability } from '@/core/module-registry';
import { apiRequest } from '@/core/api';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useWorkspace } from '@/core/workspace-context';
import { listConversations } from '@/modules/chat/api';
import { searchEvents } from '@/modules/timeline/api';
import { searchDocuments, searchEntities, type SearchFilters } from '@/modules/search/api';

type Scope = 'all' | 'entities' | 'documents' | 'events' | 'conversations';
const scopes: Scope[] = ['all', 'entities', 'documents', 'events', 'conversations'];
const scopeKeys = { all: 'scopeAll', entities: 'scopeEntities', documents: 'scopeDocuments', events: 'scopeEvents', conversations: 'scopeConversations' } as const;
const noFilters: SearchFilters = { source_ids: [], date_from: null, date_to: null, content_types: [] };
const MIN_QUERY = 2;

/** One selectable row; `disabledReason` keeps unavailable actions visible but inert. */
interface PaletteOption {
  id: string;
  group: 'actions' | 'goTo' | 'documents' | 'entities' | 'events' | 'conversations';
  label: string;
  meta?: string;
  /** Entity type shown as a pill instead of plain meta text. */
  badge?: string;
  disabledReason?: string;
  run: () => void;
}

/** Checks whether a key event originated in an editable control. */
function isEditing(target: EventTarget | null) {
  return target instanceof HTMLElement && Boolean(target.closest('input, textarea, select, [contenteditable], [role="textbox"]'));
}

/** Debounces a rapidly changing value so search requests start after typing pauses. */
function useDebounced<T>(value: T, delay = 250) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

/** Registers Ctrl/Cmd+K and renders the combobox/listbox palette for destinations, actions and search results. */
export function CommandPalette() {
  const { navigate } = useGuardedNavigation();
  const { setDraft, openDrawer, selectConversation } = useChatController();
  const t = useTranslations('shell');
  const tc = useTranslations('chat');
  const { selection } = useWorkspace();
  const inputRef = useRef<HTMLInputElement>(null);
  const openRef = useRef(false);
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const [scope, setScope] = useState<Scope>('all');
  const [activeIndex, setActiveIndex] = useState(0);
  const [online, setOnline] = useState(true);
  const query = filter.trim();
  const debounced = useDebounced(query);
  const searching = open && debounced.length >= MIN_QUERY;
  const wantsDocs = scope === 'all' || scope === 'documents';
  const wantsEntities = scope === 'all' || scope === 'entities';
  const wantsEvents = scope === 'all' || scope === 'events';
  const wantsConversations = scope === 'all' || scope === 'conversations';

  const moduleAvailability = useQuery({
    queryKey: ['module-lifecycle'],
    queryFn: () => apiRequest<ModuleAvailability>('/api/v1/settings/modules'),
    refetchOnWindowFocus: true,
  });
  const docs = useQuery({ queryKey: ['palette', 'documents', debounced], queryFn: () => searchDocuments(debounced, noFilters, 'hybrid'), enabled: searching && wantsDocs && online, retry: false });
  const entities = useQuery({ queryKey: ['palette', 'entities', debounced], queryFn: () => searchEntities(debounced), enabled: searching && wantsEntities && online, retry: false });
  const events = useQuery({ queryKey: ['palette', 'events', debounced], queryFn: () => searchEvents(debounced), enabled: searching && wantsEvents && online, retry: false });
  const conversations = useQuery({ queryKey: ['palette', 'conversations', debounced], queryFn: () => listConversations({ q: debounced, limit: 8 }), enabled: searching && wantsConversations && online, retry: false });

  useEffect(() => {
    openRef.current = open;
    if (!open) return;
    const sync = () => setOnline(navigator.onLine);
    sync();
    window.addEventListener('online', sync);
    window.addEventListener('offline', sync);
    return () => { window.removeEventListener('online', sync); window.removeEventListener('offline', sync); };
  }, [open]);

  useEffect(() => {
    /** Handles the palette shortcut only when the key event is not consumed by an editable control. */
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setOpen((value) => !value);
      } else if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !isEditing(event.target) && !openRef.current) {
        const searchField = document.querySelector<HTMLInputElement>('[data-search-query]');
        if (searchField) { event.preventDefault(); searchField.focus(); }
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  const options = useMemo<PaletteOption[]>(() => {
    /** Closes the palette and clears its transient query state. */
    const close = () => { setOpen(false); setFilter(''); setScope('all'); setActiveIndex(0); };
    /** Navigates through the guarded router and closes only when navigation was allowed. */
    const go = (href: string) => { if (navigate(href)) close(); };
    const list: PaletteOption[] = [];
    const lower = query.toLocaleLowerCase();
    if (scope === 'all' && query) {
      list.push(
        { id: 'ask-ai', group: 'actions', label: tc('paletteAskAi', { query }), run: () => { setDraft(query); openDrawer(); close(); } },
        { id: 'create-rule', group: 'actions', label: tc('paletteCreateRule'), meta: tc('paletteCreateRuleHint'), run: () => go('/settings/dashboard') },
        { id: 'collect-now', group: 'actions', label: tc('paletteCollectNow'), disabledReason: tc('paletteCollectUnavailable'), run: () => undefined },
      );
    }
    if (scope === 'all') {
      for (const dest of commandDestinations) {
        const label = t(dest.messageKey);
        if (destinationEnabled(dest, moduleAvailability.data) && destinationForRole(dest, selection?.role) && label.toLocaleLowerCase().includes(lower)) {
          list.push({ id: `go-${dest.id}`, group: 'goTo', label, run: () => go(dest.href) });
        }
      }
    }
    if (wantsDocs) for (const hit of docs.data?.items ?? []) list.push({ id: `doc-${hit.chunk_id}`, group: 'documents', label: hit.title, meta: `${hit.source.name} · v${hit.version_number}`, run: () => go(`/knowledge/documents/${hit.document_id}?versionId=${hit.document_version_id}&chunkId=${hit.chunk_id}#cited-chunk`) });
    if (wantsEntities) for (const hit of entities.data?.items ?? []) list.push({ id: `ent-${hit.id}`, group: 'entities', label: hit.name ?? hit.id, badge: hit.type, run: () => go(`/knowledge/entities/${hit.id}`) });
    if (wantsEvents) for (const hit of events.data?.items ?? []) list.push({ id: `evt-${hit.id}`, group: 'events', label: hit.title, meta: hit.type, run: () => go(`/timeline?event_id=${encodeURIComponent(hit.id)}`) });
    if (wantsConversations) {
      for (const conv of conversations.data ?? []) {
        list.push({ id: `conv-${conv.id}`, group: 'conversations', label: conv.title || tc('untitledConversation'), run: () => { selectConversation(conv.id); go('/chat'); } });
      }
    }
    return list;
  }, [query, scope, moduleAvailability.data, docs.data, entities.data, events.data, conversations.data, selection?.role, wantsDocs, wantsEntities, wantsEvents, wantsConversations, t, tc, setDraft, openDrawer, selectConversation, navigate]);

  const active = options.length ? Math.min(activeIndex, options.length - 1) : -1;
  const optionId = (index: number) => `palette-option-${options[index]?.id}`;
  const groupLabel = (group: PaletteOption['group']) => group === 'goTo' ? tc('paletteGoTo') : group === 'actions' ? tc('paletteActions') : group === 'documents' ? tc('paletteDocuments') : group === 'entities' ? tc('paletteEntities') : group === 'events' ? tc('paletteEvents') : tc('paletteConversations');
  const groups = options.reduce<{ group: PaletteOption['group']; items: { option: PaletteOption; index: number }[] }[]>((acc, option, index) => {
    const last = acc[acc.length - 1];
    if (last && last.group === option.group) last.items.push({ option, index }); else acc.push({ group: option.group, items: [{ option, index }] });
    return acc;
  }, []);

  /** Implements combobox arrow, Home/End and Enter semantics without moving DOM focus off the input. */
  const onInputKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing || !options.length) return;
    if (event.key === 'ArrowDown') { event.preventDefault(); setActiveIndex((active + 1) % options.length); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setActiveIndex((active - 1 + options.length) % options.length); }
    else if (event.key === 'Home') { event.preventDefault(); setActiveIndex(0); }
    else if (event.key === 'End') { event.preventDefault(); setActiveIndex(options.length - 1); }
    else if (event.key === 'Enter') { event.preventDefault(); const option = options[active]; if (option && !option.disabledReason) option.run(); }
  };

  const loading = searching && ((wantsDocs && docs.isFetching) || (wantsEntities && entities.isFetching) || (wantsEvents && events.isFetching) || (wantsConversations && conversations.isFetching));
  const failed = searching && ((wantsDocs && docs.isError) || (wantsEntities && entities.isError) || (wantsEvents && events.isError) || (wantsConversations && conversations.isError));
  const mode = searching && wantsDocs ? docs.data?.effective_mode : undefined;
  const needsQuery = query.length < MIN_QUERY && scope !== 'all';
  const showEmpty = !loading && !failed && options.length === 0 && !needsQuery;

  return <>
    <Button type="button" variant="outline" className="search-trigger" onClick={() => setOpen(true)} aria-label={t('searchOrRun')} aria-keyshortcuts="Control+K Meta+K"><SearchIcon className="size-4" aria-hidden="true" /><span className="search-label">{t('searchOrRun')}</span><kbd>Ctrl K</kbd></Button>
    <Dialog open={open} onOpenChange={(next) => { if (next) setOpen(true); else { setOpen(false); setFilter(''); setScope('all'); setActiveIndex(0); } }}>
      <DialogContent className="top-24 translate-y-0 gap-0 overflow-hidden rounded-2xl bg-surface p-0 sm:max-w-[680px]" closeLabel={t('close')} onOpenAutoFocus={(event) => { event.preventDefault(); inputRef.current?.focus(); }}>
        <DialogTitle className="sr-only">{tc('paletteTitle')}</DialogTitle>
        <DialogDescription className="sr-only">{t('workspaceCommands')}</DialogDescription>
        <div className="flex items-center gap-3 border-b border-border px-4 pr-12">
          <SearchIcon className="size-[18px] shrink-0 text-muted-foreground" aria-hidden="true" />
          <input
            ref={inputRef}
            role="combobox"
            aria-label={tc('paletteInputLabel')}
            aria-expanded={options.length > 0}
            aria-controls="palette-listbox"
            aria-autocomplete="list"
            aria-activedescendant={active >= 0 ? optionId(active) : undefined}
            className="min-h-14 min-w-0 flex-1 bg-transparent text-[17px] text-foreground outline-none placeholder:text-muted-foreground"
            placeholder={t('searchOrRun')}
            value={filter}
            onChange={(event) => { setFilter(event.target.value); setActiveIndex(0); }}
            onKeyDown={onInputKeyDown}
          />
        </div>
        <div role="tablist" aria-label={tc('paletteScopes')} className="flex flex-wrap items-center gap-1.5 border-b border-border px-4 py-2.5">
          {scopes.map((item) => (
            <button key={item} type="button" role="tab" aria-selected={scope === item} className={`inline-flex h-[22px] items-center rounded-full border px-2 text-xs font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${scope === item ? 'border-primary text-primary' : 'border-border text-muted-foreground hover:text-foreground'}`} onClick={() => { setScope(item); setActiveIndex(0); inputRef.current?.focus(); }}>{tc(scopeKeys[item])}</button>
          ))}
          {mode && <span className="ml-auto text-xs text-muted-foreground">{mode === 'lexical' ? tc('paletteLexical') : tc('paletteHybrid')}</span>}
        </div>
        <div className="max-h-[520px] overflow-y-auto px-2 pb-2 pt-1">
          <div role="listbox" id="palette-listbox" aria-label={tc('paletteTitle')}>
            {groups.map(({ group, items }) => (
              <div key={group} role="group" aria-label={groupLabel(group)}>
                <div className="px-3 pb-1 pt-2.5 text-xs font-semibold text-muted-foreground" aria-hidden="true">{groupLabel(group)}</div>
                {items.map(({ option, index }) => {
                  return (
                    <div key={option.id} id={optionId(index)} role="option" aria-selected={index === active} aria-disabled={option.disabledReason ? true : undefined} className={`flex min-h-11 items-center gap-3 rounded-md px-3 py-1.5 text-sm text-foreground ${index === active ? 'bg-background' : ''} ${option.disabledReason ? 'cursor-not-allowed opacity-70' : 'cursor-pointer'}`} onMouseMove={() => setActiveIndex(index)} onClick={() => { if (!option.disabledReason) option.run(); }}>
                      <span className="min-w-0 flex-1 truncate">{option.label}</span>
                      {option.badge && !option.disabledReason && <Badge variant="outline" className="shrink-0 text-muted-foreground">{option.badge}</Badge>}
                      {(option.disabledReason || option.meta) && <span className="max-w-[45%] shrink-0 truncate text-xs text-muted-foreground">{option.disabledReason ?? option.meta}</span>}
                      {index === active && !option.disabledReason && <kbd className="palette-kbd">Enter</kbd>}
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
          <div role="status" aria-live="polite" className="flex flex-col gap-1 px-2 py-1 text-xs text-muted-foreground">
            {loading && <p>{tc('paletteSearching')}</p>}
            {!loading && options.length > 0 && <p className="sr-only">{tc('paletteResultCount', { count: options.length })}</p>}
            {showEmpty && <p>{tc('paletteNoResults')}</p>}
            {(scope === 'all' || scope === 'events') && query.length >= MIN_QUERY && <p>{tc('paletteEventsNote')}</p>}
            {needsQuery && <p>{tc('paletteTypeToSearch')}</p>}
            {(scope === 'all' || scope === 'conversations') && query.length >= MIN_QUERY && <p>{tc('paletteConversationsNote')}</p>}
            {!online && <p>{tc('paletteOffline')}</p>}
            {failed && <p role="alert" className="text-destructive">{tc('paletteError')}</p>}
          </div>
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-border px-4 py-2.5 text-xs text-muted-foreground">
          <span><kbd className="palette-kbd">↑</kbd> <kbd className="palette-kbd">↓</kbd> {tc('paletteHintMove')}</span>
          <span><kbd className="palette-kbd">Enter</kbd> {tc('paletteHintOpen')}</span>
          <span><kbd className="palette-kbd">Esc</kbd> {tc('paletteHintClose')}</span>
          <span><kbd className="palette-kbd">Ctrl K</kbd> {tc('paletteHintCommands')}</span>
          <span><kbd className="palette-kbd">/</kbd> {tc('paletteHintSearch')}</span>
        </div>
      </DialogContent>
    </Dialog>
  </>;
}
