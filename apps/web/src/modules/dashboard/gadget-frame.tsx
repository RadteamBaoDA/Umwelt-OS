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
import React, { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useDisplayPreferences } from '@/core/query-provider';
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
  const ts = useTranslations('uiSweep');
  const { openDrawer } = useChatController();
  const display = useDisplayPreferences();
  const [collectedAt, setCollectedAt] = useState<string | null>(null);
  const [isExpanded, setIsExpanded] = useState<boolean>(false);
  const [rendererHost, setRendererHost] = useState<HTMLDivElement | null>(null);
  const rendererHostRef = useRef<HTMLDivElement | null>(null);
  const inlineHostRef = useRef<HTMLDivElement>(null);
  const expandedHostRef = useRef<HTMLDivElement>(null);
  const inlineScrollRef = useRef<HTMLDivElement>(null);
  const inlineScrollPosition = useRef(0);
  const expandTriggerRef = useRef<HTMLButtonElement>(null);
  const headerRef = useRef<HTMLElement>(null);
  const titleId = useId();
  const bodyFloor = getGadgetReadingBodyFloor(instance.definition.renderer);

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

  /** Reports header border-box height; its fixed single-row structure excludes streamed queue counts. */
  useEffect(() => {
    const node = headerRef.current;
    if (!node || !onHeaderMeasured) return;
    /** Reports the current stable header border-box height to the dashboard grid. */
    const report = () => onHeaderMeasured(node.getBoundingClientRect().height);
    report();
    const observer = new ResizeObserver(report);
    observer.observe(node);
    return () => observer.disconnect();
  }, [onHeaderMeasured]);

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
  }, [rendererHost]);

  /** Moves the host as soon as Radix attaches its lazily mounted expanded destination. */
  const attachExpandedHost = useCallback((node: HTMLDivElement | null) => {
    expandedHostRef.current = node;
    if (node && isExpanded) moveRendererHost(node, true);
  }, [isExpanded, moveRendererHost]);

  /** Reparents the single portal host when the inline or expanded destination is ready. */
  useLayoutEffect(() => {
    const destination = isExpanded ? expandedHostRef.current : inlineHostRef.current;
    moveRendererHost(destination, isExpanded);
  }, [isExpanded, moveRendererHost]);

  const definition = instance.definition;
  // Freshness comes only from data the renderer actually loaded; absent means no badge.
  const collectedMs = collectedAt ? Date.parse(collectedAt) : NaN;
  const freshnessLabel = Number.isFinite(collectedMs) ? t('freshnessUpdated', { time: formatAgo(collectedMs, display.locale) }) : null;
  const freshnessTitle = collectedAt ? formatDateTime(collectedAt, display.locale, display.timezone) : undefined;
  const displayTitle = instance.title || definition.name || definition.renderer;

  // Derive most severe highlight rule or rule match
  const effectiveSeverity = ruleMatch?.severity || definition.highlight_rules?.[0]?.severity;

  /** Toggles expanded fullscreen modal view without modifying underlying grid layout. */
  const toggleExpanded = useCallback(() => {
    setIsExpanded((prev) => !prev);
  }, []);

  /** Opens the quick-chat drawer with this gadget's source IDs and scope attached as grounding context. */
  const handleAskWithUmwelt = useCallback(() => {
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

  /** Supplies exactly one renderer subtree to the stable host; it is never invoked in two surfaces. */
  const renderGadgetContent = () => {
    if (children) {
      return children;
    }

    // Default renderer card for R10/R11 planned state
    return (
      <div className="flex flex-col h-full justify-between p-4 bg-card text-card-foreground">
        <div className="space-y-3">
          <div className="flex items-center justify-between text-xs text-muted-foreground border-b border-border pb-2">
            <span className="font-mono uppercase tracking-wider">{definition.renderer}</span>
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
                    className="px-1.5 py-0.5 rounded bg-muted/30 text-muted-foreground font-mono"
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
          <span className="font-mono text-[10px]">rev.{definition.revision}</span>
        </div>
      </div>
    );
  };

  return (
    <>
      <Card
        role="region"
        aria-labelledby={titleId}
        tabIndex={0}
        aria-label={t('gadgetScrollRegionLabel', { title: displayTitle })}
        onClick={isEditMode ? onSelect : undefined}
        className={`dashboard-gadget group relative h-full w-full gap-0 rounded-lg py-0 overflow-hidden focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
          isSelected ? 'ring-2 ring-primary border-primary' : 'border-border'
        } ${isDragging ? 'opacity-40 shadow-lg scale-[0.99]' : ''} ${
          isResizing ? 'ring-1 ring-primary/60' : ''
        }`}
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

          {/* Status badges share the row; overflow is clipped so streaming counts never change grid height. */}
          <div className="hidden min-w-0 shrink items-center gap-1.5 overflow-hidden whitespace-nowrap @[22rem]:flex">
            {unreadCount > 0 && <Badge className="shrink-0 px-1.5 text-[11px]" title={ts('frameUnread', { count: unreadCount })}>{t('unreadBadge', { count: unreadCount })}</Badge>}
            {ruleMatch?.matched && <Badge variant="outline" className="shrink-0 gap-1 border-primary/30 bg-primary/10 text-[11px] text-foreground" title={ruleMatch.reason || t('ruleMatchFallback')}><AlertTriangle className="w-3 h-3" /><span>{t('ruleMatchBadge')}</span></Badge>}
            {effectiveSeverity && <Badge variant={effectiveSeverity === 'critical' ? 'destructive' : 'outline'} className={`shrink-0 text-[10px] font-bold uppercase tracking-wider ${effectiveSeverity === 'info' ? 'text-muted-foreground' : ''}`}>{effectiveSeverity === 'critical' ? t('severityCritical') : effectiveSeverity === 'warning' ? t('severityWarning') : t('severityInfo')}</Badge>}
            {definition.warnings && definition.warnings.length > 0 && <Badge variant="secondary" className="shrink-0 gap-0.5 text-[10px]" title={ts('frameSetupTitle')}><Info className="w-3 h-3" /><span>{t('setupRequired')}</span></Badge>}
          </div>

          {/* Action controls cannot start title dragging. */}
          <div className="flex shrink-0 flex-nowrap items-center gap-1" onPointerDown={(event) => event.stopPropagation()}>
            {!isEditMode && (
              <Button
                type="button"
                onClick={handleAskWithUmwelt}
                aria-label={t('askWithUmwelt')}
                title={t('askWithUmwelt')}
                data-testid="gadget-ask-with-umwelt"
                variant="ghost"
                size="sm"
                className="h-8 gap-1.5 rounded-md px-2 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground"
              >
                <Sparkles className="size-3.5 text-muted-foreground" aria-hidden="true" />
                <span className="hidden @[26rem]:inline">{t('askWithUmwelt')}</span>
              </Button>
            )}
            {/* Expand / Maximize modal trigger */}
            <Button
              type="button"
              ref={expandTriggerRef}
              onClick={toggleExpanded}
              aria-label={t('expand')}
              variant="ghost"
              size="icon"
              className="size-9 min-h-9 min-w-9 rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground"
            >
              <Maximize2 className="size-4" aria-hidden="true" />
            </Button>

            {/* Edit mode configuration and removal actions */}
            {isEditMode && (
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
            )}
          </div>
        </header>

        <div ref={inlineScrollRef} className="dashboard-gadget-body min-h-0 flex-1 overflow-auto overscroll-contain" style={{ minHeight: bodyFloor }}>
        {/* Queue controls scroll inside this fixed-floor body and never change the grid geometry. */}
        {readingQueueCount > 0 && onApplyReadingQueue && (
          <div className="px-3 py-1.5 bg-raised border-b border-rule text-foreground text-xs flex items-center justify-between">
            <span className="font-medium">
              {t('readingUpdatesQueued', { count: readingQueueCount })}
            </span>
            <Button
              type="button"
              className="min-h-11"
              onClick={onApplyReadingQueue}
            >
              {t('showUpdates')}
            </Button>
          </div>
        )}

        <div ref={inlineHostRef} className="h-full min-h-full min-w-0" />
        </div>
        {/* Provenance footer: a fixed one-line row inside the flex column; hidden when freshness is unknown. */}
        {freshnessLabel && (
          <p className="m-0 shrink-0 truncate border-t border-rule px-3.5 py-1 text-[11px] leading-4 text-muted-foreground" title={freshnessTitle} data-testid="gadget-provenance">{freshnessLabel}</p>
        )}
      </Card>

      {/* Expanded Modal View (preserves filters, scroll position and local state) */}
      <Dialog open={isExpanded} onOpenChange={setIsExpanded}>
        <DialogContent closeLabel={t('close')} onCloseAutoFocus={(event) => { event.preventDefault(); expandTriggerRef.current?.focus(); }} className="flex h-[calc(100dvh-4rem)] max-h-[calc(100dvh-4rem)] w-[calc(100vw-2rem)] flex-col gap-0 overflow-hidden rounded-2xl p-0 sm:max-w-[1240px]">
          <DialogHeader className="shrink-0 gap-0 border-b border-border py-4 pl-6 pr-16 text-left">
            <DialogTitle className="text-lg font-semibold leading-snug">{displayTitle}</DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground">
              {definition.renderer} {'·'} {t('gadgetExpandedDescription', { title: displayTitle })}
            </DialogDescription>
          </DialogHeader>
          <div ref={attachExpandedHost} className="min-h-0 flex-1 overflow-auto overscroll-contain p-6" />
        </DialogContent>
      </Dialog>
      {rendererHost && createPortal(<GadgetFreshnessProvider report={setCollectedAt}>{renderGadgetContent()}</GadgetFreshnessProvider>, rendererHost)}
    </>
  );
}
