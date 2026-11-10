'use client';

import {
  AlertTriangle,
  GripHorizontal,
  Info,
  Maximize2,
  MoreVertical,
  Settings,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { useQueryClient } from '@tanstack/react-query';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { collectAndWait, collectErrorKey, sourceKeys } from '@/modules/sources';
import type { FreshnessTick } from './freshness-strip';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useDisplayPreferences } from '@/core/query-provider';
import { templateName } from '@/core/renderer-labels';
import { formatDateTime } from '@/core/i18n';
import { GadgetFreshnessProvider } from './gadget-freshness';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import type { GadgetInstance } from './api';
import { getGadgetReadingBodyFloor } from './widget-registry';

/** Rule match observation metadata displayed on gadget cards. */
export interface GadgetRuleMatch {
  matched: boolean;
  ruleId?: string;
  reason?: string;
  severity?: 'info' | 'warning' | 'critical';
}

/** Configuration and interactive handlers for an individual gadget container. */
export interface GadgetFrameProps {
  /** Freshness ticks of the sources this gadget reads; a stale or failing one shows a badge with a Collect now action. */
  sourceHealth?: readonly FreshnessTick[];
  /** The gadget instance configuration and definition projection. */
  instance: GadgetInstance;
  /** Whether the parent dashboard grid is currently in layout edit mode. */
  isEditMode: boolean;
  /** Indicates whether this gadget is currently being dragged on the grid. */
  isDragging?: boolean;
  /** Indicates whether this gadget is currently being resized on the grid. */
  isResizing?: boolean;
  /** Indicates whether this gadget has keyboard or pointer focus in edit mode. */
  isSelected?: boolean;
  /** Optional unread items count for feed and message gadgets. */
  unreadCount?: number;
  /** Active rule highlight matching metadata for badge rendering. */
  ruleMatch?: GadgetRuleMatch | null;
  /** Number of incoming items queued to prevent jumping while user is reading. */
  readingQueueCount?: number;
  /** Callback fired when the user chooses to reveal/apply queued reading updates. */
  onApplyReadingQueue?: () => void;
  /** Selection handler for keyboard navigation in edit mode. */
  onSelect?: () => void;
  /** Handler to open instance and definition configuration modal. */
  onConfigure?: () => void;
  /** Handler to remove this gadget instance from the active dashboard. */
  onRemove?: () => void;
  /** Pointer down handler initiating title-based drag to move in edit mode. */
  onDragHandlePointerDown?: (event: React.PointerEvent<HTMLDivElement>) => void;
  /** Custom gadget renderer body if provided by caller. */
  children?: React.ReactNode;
  /** Reports measured stable frame header chrome so mobile reading projection uses actual pixels. */
  onHeaderMeasured?: (height: number) => void;
}

/**
 * Quiet-chrome classes for gadget header actions: hidden until the gadget is hovered or holds focus,
 * always visible on coarse pointers where hover does not exist. Focus-within keeps keyboard users unblocked.
 */
const REVEAL_ON_HOVER = 'opacity-0 transition-opacity group-hover/gadget:opacity-100 group-focus-within/gadget:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100';

/**
 * Formats an elapsed time as a localized relative phrase (minutes, hours or days).
 *
 * @param ms Epoch milliseconds of the event.
 * @param locale Display locale id.
 * @returns Relative phrase such as "2 minutes ago".
 */
function formatAgo(ms: number, locale: string): string {
  const minutes = Math.round((Date.now() - ms) / 60_000);
  const rtf = new Intl.RelativeTimeFormat(locale === 'vi-vi' ? 'vi' : 'en', { numeric: 'auto' });
  if (Math.abs(minutes) < 60) return rtf.format(-Math.max(0, minutes), 'minute');
  if (Math.abs(minutes) < 1440) return rtf.format(-Math.round(minutes / 60), 'hour');
  return rtf.format(-Math.round(minutes / 1440), 'day');
}

/**
 * Owns the single renderer host element and moves it between the inline body and the expanded dialog without
 * remounting the gadget, restoring the inline scroll position when it returns.
 *
 * @param isExpanded Whether the expanded dialog is open.
 * @param refs Inline host, inline scroll container and the expanded host (owned by the frame component).
 * @returns The host element once created and the callback ref for the lazily mounted expanded destination.
 */
function useRendererHost(
  isExpanded: boolean,
  refs: { inlineHostRef: RefObject<HTMLDivElement | null>; inlineScrollRef: RefObject<HTMLDivElement | null>; expandedHostRef: RefObject<HTMLDivElement | null> },
) {
  const { inlineHostRef, inlineScrollRef, expandedHostRef } = refs;
  const [rendererHost, setRendererHost] = useState<HTMLDivElement | null>(null);
  const rendererHostRef = useRef<HTMLDivElement | null>(null);
  const inlineScrollPosition = useRef(0);

  /** Creates one renderer host so expansion can move its DOM container without remounting the gadget. */
  useEffect(() => {
    if (!rendererHostRef.current) {
      rendererHostRef.current = document.createElement('div');
      rendererHostRef.current.style.height = '100%';
      rendererHostRef.current.style.minHeight = '100%';
      rendererHostRef.current.style.width = '100%';
      rendererHostRef.current.style.minWidth = '0';
      setRendererHost(rendererHostRef.current);
    }
  }, []);

  /** Moves the one renderer host and restores inline reading position after returning from Expand. */
  const moveRendererHost = useCallback((destination: HTMLDivElement | null, expanded: boolean) => {
    if (!rendererHost || !destination || rendererHost.parentElement === destination) return;
    if (expanded && inlineScrollRef.current) {
      inlineScrollPosition.current = inlineScrollRef.current.scrollTop;
    }
    destination.appendChild(rendererHost);
    if (!expanded && inlineScrollRef.current) {
      inlineScrollRef.current.scrollTop = inlineScrollPosition.current;
    }
  }, [inlineScrollRef, rendererHost]);

  /** Moves the host as soon as Radix attaches its lazily mounted expanded destination. */
  const attachExpandedHost = useCallback((node: HTMLDivElement | null) => {
    expandedHostRef.current = node;
    if (node && isExpanded) moveRendererHost(node, true);
  }, [expandedHostRef, isExpanded, moveRendererHost]);

  /** Reparents the single portal host when the inline or expanded destination is ready. */
  useLayoutEffect(() => {
    const destination = isExpanded ? expandedHostRef.current : inlineHostRef.current;
    moveRendererHost(destination, isExpanded);
  }, [expandedHostRef, inlineHostRef, isExpanded, moveRendererHost]);

  return { rendererHost, attachExpandedHost };
}

/** Reports header border-box height; its fixed single-row structure excludes streamed queue counts. */
function useHeaderMeasurement(headerRef: RefObject<HTMLElement | null>, onHeaderMeasured?: (height: number) => void) {
  useEffect(() => {
    const node = headerRef.current;
    if (!node || !onHeaderMeasured) return;
    /** Reports the current stable header border-box height to the dashboard grid. */
    const report = () => onHeaderMeasured(node.getBoundingClientRect().height);
    report();
    const observer = new ResizeObserver(report);
    observer.observe(node);
    return () => observer.disconnect();
  }, [headerRef, onHeaderMeasured]);
}

/**
 * Renderer-reported freshness and attribution for the footer line.
 * Freshness comes only from data the renderer actually loaded; absent means no badge.
 */
function useGadgetFooter() {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const [collectedAt, setCollectedAt] = useState<string | null>(null);
  const [attribution, setAttribution] = useState<string | null>(null);
  /** Receives renderer-reported freshness and optional attribution for the footer line. */
  const reportFreshness = useCallback((at: string | null, attr?: string | null) => { setCollectedAt(at); setAttribution(attr ?? null); }, []);
  const collectedMs = collectedAt ? Date.parse(collectedAt) : NaN;
  const freshnessLabel = Number.isFinite(collectedMs) ? t('freshnessUpdated', { time: formatAgo(collectedMs, display.locale) }) : null;
  const freshnessTitle = collectedAt ? formatDateTime(collectedAt, display.locale, display.timezone) : undefined;
  const footerText = [attribution, freshnessLabel].filter(Boolean).join(' · ');
  return { reportFreshness, footerText, freshnessTitle };
}

/** Highlight severity badge: critical is destructive, info is muted. */
function SeverityBadge({ severity }: { severity: 'info' | 'warning' | 'critical' }) {
  const t = useTranslations('dashboard');
  return (
    <Badge variant={severity === 'critical' ? 'destructive' : 'outline'} className={`shrink-0 text-xs font-semibold ${severity === 'info' ? 'text-muted-foreground' : ''}`}>
      {severity === 'critical' ? t('severityCritical') : severity === 'warning' ? t('severityWarning') : t('severityInfo')}
    </Badge>
  );
}

/**
 * Stale/failing badge for the gadget's bound sources plus a "Collect now" action for the worst one (failing first).
 * A failing source shows its plain-language reason (`collectErrorKey`, raw code as fallback) as the badge title.
 * Collect now triggers one collection with the owner CSRF token, waits for the terminal state and then refreshes
 * every source query; failures are announced, never thrown. Renders nothing while every source is fresh.
 *
 * @param sourceHealth Freshness ticks of the bound sources.
 */
function SourceHealthBadge({ sourceHealth }: { sourceHealth: readonly FreshnessTick[] }) {
  const t = useTranslations('dashboard');
  const ts = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [state, setState] = useState<'idle' | 'running' | 'failed'>('idle');
  const worst = sourceHealth.find((tick) => tick.status === 'failing') ?? sourceHealth.find((tick) => tick.status === 'stale');
  if (!worst) return null;
  const failing = worst.status === 'failing';
  const key = failing ? collectErrorKey(worst.errorCode) : null;
  const reason = failing ? (key ? ts(key) : worst.errorCode ?? '') : t('sourceStaleTitle');
  /** Runs one collection for the worst source and refreshes the freshness data once it settles. */
  const collect = async () => {
    setState('running');
    try {
      const result = await collectAndWait(worst.id, csrfToken, { signal: new AbortController().signal });
      setState(result && result.status !== 'succeeded' && result.status !== 'no_changes' ? 'failed' : 'idle');
    } catch { setState('failed'); }
    await client.invalidateQueries({ queryKey: sourceKeys.list });
  };
  return (
    <>
      <Badge variant={failing ? 'destructive' : 'secondary'} className="shrink-0 gap-1 text-[11px]" title={reason}>
        <AlertTriangle className="w-3 h-3" aria-hidden /><span>{t(failing ? 'sourceFailingBadge' : 'sourceStaleBadge')}</span>
      </Badge>
      <Button type="button" size="sm" variant="ghost" className="h-6 shrink-0 px-1.5 text-[11px]" disabled={state === 'running'} onClick={() => void collect()}>
        {state === 'running' ? ts('collecting') : ts('collectNow')}
      </Button>
      {state === 'failed' && <span role="alert" className="shrink-0 text-[11px] text-destructive">{t('sourceCollectFailed')}</span>}
    </>
  );
}

/** Status badges that share the header row; overflow is clipped so streaming counts never change grid height. */
function StatusBadges({ instance, unreadCount, ruleMatch, sourceHealth }: Pick<GadgetFrameProps, 'instance' | 'unreadCount' | 'ruleMatch' | 'sourceHealth'>) {
  const t = useTranslations('dashboard');
  const ts = useTranslations('uiSweep');
  const definition = instance.definition;
  // Derive most severe highlight rule or rule match
  const effectiveSeverity = ruleMatch?.severity || definition.highlight_rules?.[0]?.severity;
  const unread = unreadCount ?? 0;
  return (
    <div className="hidden min-w-0 shrink items-center gap-1.5 overflow-hidden whitespace-nowrap @[22rem]:flex">
      {unread > 0 && <Badge className="shrink-0 px-1.5 text-[11px]" title={ts('frameUnread', { count: unread })}>{t('unreadBadge', { count: unread })}</Badge>}
      {ruleMatch?.matched && <Badge variant="outline" className="shrink-0 gap-1 border-primary/30 bg-primary/10 text-[11px] text-foreground" title={ruleMatch.reason || t('ruleMatchFallback')}><AlertTriangle className="w-3 h-3" /><span>{t('ruleMatchBadge')}</span></Badge>}
      {sourceHealth && sourceHealth.length > 0 && <SourceHealthBadge sourceHealth={sourceHealth} />}
      {effectiveSeverity && <SeverityBadge severity={effectiveSeverity} />}
      {definition.warnings && definition.warnings.length > 0 && <Badge variant="secondary" className="shrink-0 gap-0.5 text-xs" title={ts('frameSetupTitle')}><Info className="w-3 h-3" /><span>{t('setupRequired')}</span></Badge>}
    </div>
  );
}

/** Edit-mode configuration and removal menu. */
function EditMenu({ onConfigure, onRemove }: Pick<GadgetFrameProps, 'onConfigure' | 'onRemove'>) {
  const t = useTranslations('dashboard');
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          aria-label={t('configureGadget')}
          variant="ghost"
          size="icon"
          className="size-9 min-h-9 min-w-9 rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground"
        >
          <MoreVertical className="size-4" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-44">
        {onConfigure && (
          <DropdownMenuItem onClick={onConfigure} className="gap-2 text-xs">
            <Settings className="w-3.5 h-3.5" />
            <span>{t('configureGadget')}</span>
          </DropdownMenuItem>
        )}
        {onRemove && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onClick={onRemove}
              className="gap-2 text-xs text-destructive focus:text-destructive"
            >
              <Trash2 className="w-3.5 h-3.5" />
              <span>{t('removeGadget')}</span>
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Header action controls; they cannot start title dragging. */
function HeaderActions({ isEditMode, onAsk, onExpand, expandRef, onConfigure, onRemove }: {
  isEditMode: boolean;
  onAsk: () => void;
  onExpand: () => void;
  expandRef: RefObject<HTMLButtonElement | null>;
} & Pick<GadgetFrameProps, 'onConfigure' | 'onRemove'>) {
  const t = useTranslations('dashboard');
  return (
    <div className="flex shrink-0 flex-nowrap items-center gap-1" onPointerDown={(event) => event.stopPropagation()}>
      {!isEditMode && (
        <Button
          type="button"
          onClick={onAsk}
          aria-label={t('askWithUmwelt')}
          title={t('askWithUmwelt')}
          data-testid="gadget-ask-with-umwelt"
          variant="ghost"
          size="icon"
          className={`${REVEAL_ON_HOVER} size-7 min-h-7 min-w-7 rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground`}
        >
          <Sparkles className="size-3.5 text-muted-foreground" aria-hidden="true" />
        </Button>
      )}
      {/* Expand / Maximize modal trigger */}
      <Button
        type="button"
        ref={expandRef}
        onClick={onExpand}
        aria-label={t('expand')}
        variant="ghost"
        size="icon"
        className={`${isEditMode ? '' : REVEAL_ON_HOVER} size-7 min-h-7 min-w-7 rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground`}
      >
        <Maximize2 className="size-4" aria-hidden="true" />
      </Button>

      {/* Edit mode configuration and removal actions */}
      {isEditMode && <EditMenu onConfigure={onConfigure} onRemove={onRemove} />}
    </div>
  );
}

/** Default renderer card for the planned state: renderer id, linked sources, keywords and scope chips. */
function PlannedGadgetBody({ instance }: { instance: GadgetInstance }) {
  const ts = useTranslations('uiSweep');
  const definition = instance.definition;
  return (
    <div className="flex flex-col h-full justify-between p-4 bg-card text-card-foreground">
      <div className="space-y-3">
        <div className="flex items-center justify-between text-xs text-muted-foreground border-b border-border pb-2">
          <span>{definition.renderer}</span>
          <span>{definition.source_ids.length} sources linked</span>
        </div>

        {definition.filters?.keywords && definition.filters.keywords.length > 0 && (
          <div className="text-xs">
            <span className="text-muted-foreground">{ts('frameFilters')}</span>
            <span className="font-medium text-foreground">
              {definition.filters.keywords.join(', ')}
            </span>
          </div>
        )}

        {definition.scope && Object.keys(definition.scope).length > 0 && (
          <div className="flex flex-wrap gap-1 text-[11px]">
            {Object.entries(definition.scope).map(([key, values]) => {
              if (!values || !Array.isArray(values) || values.length === 0) return null;
              return (
                <span
                  key={key}
                  className="px-1.5 py-0.5 rounded bg-muted/30 text-muted-foreground"
                >
                  {key}: {values.join(', ')}
                </span>
              );
            })}
          </div>
        )}
      </div>

      <div className="pt-3 border-t border-border mt-3 text-xs text-muted-foreground flex items-center justify-between">
        <span className="italic">{ts('frameLive')}</span>
        <span className="tabular-nums text-xs">rev.{definition.revision}</span>
      </div>
    </div>
  );
}

/** Banner of incoming items queued while the user reads; scrolls inside the fixed-floor body. */
function ReadingQueueBar({ count, onApply }: { count: number; onApply: () => void }) {
  const t = useTranslations('dashboard');
  return (
    <div className="px-3 py-1.5 bg-raised border-b border-rule text-foreground text-xs flex items-center justify-between">
      <span className="font-medium">
        {t('readingUpdatesQueued', { count })}
      </span>
      <Button type="button" onClick={onApply}>
        {t('showUpdates')}
      </Button>
    </div>
  );
}

/** Expanded dialog: only the destination container; the renderer host is moved into it by the frame. */
function ExpandedDialog({ open, onOpenChange, title, triggerRef, attachHost }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  triggerRef: RefObject<HTMLButtonElement | null>;
  attachHost: (node: HTMLDivElement | null) => void;
}) {
  const t = useTranslations('dashboard');
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent closeLabel={t('close')} onCloseAutoFocus={(event) => { event.preventDefault(); triggerRef.current?.focus(); }} className="flex h-[calc(100dvh-4rem)] max-h-[calc(100dvh-4rem)] w-[calc(100vw-2rem)] flex-col gap-0 overflow-hidden rounded-lg p-0 sm:max-w-[1240px]">
        <DialogHeader className="shrink-0 gap-0 border-b border-border py-4 pl-6 pr-16 text-left">
          <DialogTitle className="text-lg font-semibold leading-snug">{title}</DialogTitle>
          <DialogDescription className="text-xs text-muted-foreground">
            {t('gadgetExpandedDescription', { title })}
          </DialogDescription>
        </DialogHeader>
        <div ref={attachHost} className="min-h-0 flex-1 overflow-auto overscroll-contain p-6" />
      </DialogContent>
    </Dialog>
  );
}

/** Provenance footer: a fixed one-line row inside the flex column; hidden when freshness is unknown. */
function ProvenanceFooter({ text, title }: { text: string; title: string | undefined }) {
  if (!text) return null;
  return <p className="m-0 shrink-0 truncate border-t border-rule px-3.5 py-1 text-[11px] leading-4 text-muted-foreground" title={title} data-testid="gadget-provenance">{text}</p>;
}

/** Card chrome classes: selection ring, drag dimming and resize ring. */
function cardClassName(isSelected: boolean, isDragging: boolean, isResizing: boolean): string {
  return `dashboard-gadget group group/gadget relative h-full w-full gap-0 rounded-lg py-0 overflow-hidden focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
    isSelected ? 'ring-2 ring-primary border-primary' : 'border-border'
  } ${isDragging ? 'opacity-40 shadow-[var(--shadow-popover)] scale-[0.99]' : ''} ${
    isResizing ? 'ring-1 ring-primary/60' : ''
  }`;
}

/** Gadget title: the instance title, else the definition name, else the localized template name (never the raw renderer id). */
function useDisplayTitle(instance: GadgetInstance): string {
  const tg = useTranslations('gadgetSettings');
  return instance.title || instance.definition.name || templateName(instance.definition.renderer, tg);
}

/** Opens the quick-chat drawer with this gadget's source IDs and scope attached as grounding context. */
function useAskWithUmwelt(instance: GadgetInstance, displayTitle: string) {
  const { openDrawer } = useChatController();
  const definition = instance.definition;
  return useCallback(() => {
    openDrawer({
      context: {
        kind: 'gadget',
        gadgetId: instance.id,
        title: displayTitle,
        renderer: definition.renderer,
        sourceIds: definition.source_ids ?? [],
        scope: definition.scope ?? {},
      },
    });
  }, [openDrawer, instance.id, displayTitle, definition.renderer, definition.source_ids, definition.scope]);
}

/**
 * Standard container card wrapping each dashboard gadget. Keeps fixed header/status geometry,
 * scrolls compact cards at the outer labelled region, and reparents one renderer portal host for
 * Expand so filters, scroll position, and component state survive without layout mutation.
 *
 * @param props Gadget configuration, edit mode flags, and interactive callbacks.
 * @returns Accessible bounded gadget region with stable header measurement and a single renderer host.
 */
export function GadgetFrame({
  instance,
  isEditMode,
  isDragging = false,
  isResizing = false,
  isSelected = false,
  unreadCount = 0,
  sourceHealth,
  ruleMatch,
  readingQueueCount = 0,
  onApplyReadingQueue,
  onSelect,
  onConfigure,
  onRemove,
  onDragHandlePointerDown,
  onHeaderMeasured,
  children,
}: GadgetFrameProps) {
  const t = useTranslations('dashboard');
  const [isExpanded, setIsExpanded] = useState<boolean>(false);
  const inlineHostRef = useRef<HTMLDivElement>(null);
  const expandedHostRef = useRef<HTMLDivElement>(null);
  const inlineScrollRef = useRef<HTMLDivElement>(null);
  const expandTriggerRef = useRef<HTMLButtonElement>(null);
  const headerRef = useRef<HTMLElement>(null);
  const titleId = useId();
  const definition = instance.definition;
  const bodyFloor = getGadgetReadingBodyFloor(definition.renderer);
  const { rendererHost, attachExpandedHost } = useRendererHost(isExpanded, { inlineHostRef, inlineScrollRef, expandedHostRef });
  useHeaderMeasurement(headerRef, onHeaderMeasured);
  const { reportFreshness, footerText, freshnessTitle } = useGadgetFooter();
  const displayTitle = useDisplayTitle(instance);

  /** Toggles expanded fullscreen modal view without modifying underlying grid layout. */
  const toggleExpanded = useCallback(() => {
    setIsExpanded((prev) => !prev);
  }, []);
  const handleAskWithUmwelt = useAskWithUmwelt(instance, displayTitle);

  return (
    <>
      <Card
        role="region"
        aria-labelledby={titleId}
        tabIndex={0}
        aria-label={t('gadgetScrollRegionLabel', { title: displayTitle })}
        onClick={isEditMode ? onSelect : undefined}
        className={cardClassName(isSelected, isDragging, isResizing)}
      >
        {/* Gadget Header (artboard): one row with title, status badges and actions. */}
        <header ref={headerRef} className={`@container flex shrink-0 items-center gap-2 px-3.5 py-1.5 border-b border-border select-none ${isEditMode ? 'bg-muted/20' : 'bg-card'}`}>
          <div
            className={`flex min-w-0 flex-1 items-center gap-2 ${isEditMode ? 'cursor-grab active:cursor-grabbing hover:bg-muted/30' : ''}`}
            onPointerDown={isEditMode ? onDragHandlePointerDown : undefined}
          >
            {isEditMode && <GripHorizontal className="w-4 h-4 text-muted-foreground shrink-0 opacity-70 group-hover:opacity-100" />}
            <h3 id={titleId} className="min-w-0 truncate font-semibold text-sm text-foreground leading-tight" title={displayTitle}>
              {displayTitle}
            </h3>
          </div>
          <StatusBadges instance={instance} unreadCount={unreadCount} ruleMatch={ruleMatch} sourceHealth={sourceHealth} />
          <HeaderActions
            isEditMode={isEditMode} onAsk={handleAskWithUmwelt} onExpand={toggleExpanded} expandRef={expandTriggerRef}
            onConfigure={onConfigure} onRemove={onRemove}
          />
        </header>

        <div ref={inlineScrollRef} className="dashboard-gadget-body @container/gadget min-h-0 flex-1 overflow-auto overscroll-contain">
        {/* Queue controls scroll inside this fixed-floor body and never change the grid geometry. */}
        {readingQueueCount > 0 && onApplyReadingQueue && <ReadingQueueBar count={readingQueueCount} onApply={onApplyReadingQueue} />}

        {/* The reading floor sits on the host, not the body: a card shorter than the floor scrolls its body instead of clipping the bottom (footers, last rows) under the card edge. */}
        {/* Floor is a CSS variable so a renderer whose root IS a GadgetNotice (empty/setup/error; a direct child of this host) drops it via :has(>...) and the body shrinks to the notice. The grid item's height stays fixed by the layout; only the body area gives way. Notices nested deeper (inline banners) deliberately keep the floor. */}
        <div ref={inlineHostRef} className="h-full min-h-[var(--gadget-floor)] min-w-0 has-[>[data-gadget-notice]]:h-auto has-[>[data-gadget-notice]]:min-h-0" style={{ '--gadget-floor': `${bodyFloor}px` } as React.CSSProperties} />
        </div>
        <ProvenanceFooter text={footerText} title={freshnessTitle} />
      </Card>

      {/* Expanded Modal View (preserves filters, scroll position and local state) */}
      <ExpandedDialog open={isExpanded} onOpenChange={setIsExpanded} title={displayTitle} triggerRef={expandTriggerRef} attachHost={attachExpandedHost} />
      {/* Supplies exactly one renderer subtree to the stable host; it is never invoked in two surfaces. */}
      {rendererHost && createPortal(<GadgetFreshnessProvider report={reportFreshness}>{children ? children : <PlannedGadgetBody instance={instance} />}</GadgetFreshnessProvider>, rendererHost)}
    </>
  );
}
