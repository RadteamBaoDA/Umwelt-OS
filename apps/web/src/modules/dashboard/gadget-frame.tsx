'use client';

import {
  AlertTriangle,
  GripHorizontal,
  Info,
  Maximize2,
  MoreVertical,
  Settings,
  Trash2,
  X,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Button } from '@/components/ui/button';
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

  /** Reports header border-box height; its fixed two-row structure excludes streamed queue counts. */
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
  const displayTitle = instance.title || definition.name || definition.renderer;

  // Derive most severe highlight rule or rule match
  const effectiveSeverity = ruleMatch?.severity || definition.highlight_rules?.[0]?.severity;

  /** Toggles expanded fullscreen modal view without modifying underlying grid layout. */
  const toggleExpanded = useCallback(() => {
    setIsExpanded((prev) => !prev);
  }, []);

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
              <span className="text-muted-foreground">Filters: </span>
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
          <span className="italic">Ready for live streaming</span>
          <span className="font-mono text-[10px]">rev.{definition.revision}</span>
        </div>
      </div>
    );
  };

  return (
    <>
      <div
        role="region"
        aria-labelledby={titleId}
        tabIndex={0}
        aria-label={t('gadgetScrollRegionLabel', { title: displayTitle })}
        onClick={isEditMode ? onSelect : undefined}
        className={`group relative flex flex-col h-full w-full rounded-xl border bg-card text-card-foreground shadow-xs transition-shadow overflow-x-hidden overflow-y-auto focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
          isSelected ? 'ring-2 ring-primary border-primary' : 'border-border'
        } ${isDragging ? 'opacity-40 shadow-lg scale-[0.99]' : ''} ${
          isResizing ? 'ring-1 ring-primary/60' : ''
        }`}
      >
        {/* Gadget Header */}
        <header ref={headerRef} className={`flex shrink-0 flex-col items-stretch gap-1 px-3.5 py-2.5 border-b border-border select-none ${isEditMode ? 'bg-muted/20' : 'bg-card'}`}>
          <div className="flex min-h-11 items-center justify-between gap-2">
            <div
              className={`flex min-w-0 flex-1 items-center gap-2 ${isEditMode ? 'cursor-grab active:cursor-grabbing hover:bg-muted/30' : ''}`}
              onPointerDown={isEditMode ? onDragHandlePointerDown : undefined}
            >
              {isEditMode && <GripHorizontal className="w-4 h-4 text-muted-foreground shrink-0 opacity-70 group-hover:opacity-100" />}
              <h3 id={titleId} className="min-w-0 truncate font-semibold text-sm text-foreground leading-tight" title={displayTitle}>
                {displayTitle}
              </h3>
            </div>

            {/* Action controls occupy a fixed row and cannot start title dragging. */}
            <div className="flex shrink-0 flex-nowrap items-center gap-1 overflow-x-auto" onPointerDown={(event) => event.stopPropagation()}>
            {/* Expand / Maximize modal trigger */}
            <Button
              type="button"
              ref={expandTriggerRef}
              onClick={toggleExpanded}
              aria-label={t('expand')}
              className="min-h-11 min-w-11 rounded-md text-muted-foreground hover:text-foreground hover:bg-primary/10 transition-colors"
            >
              <Maximize2 className="w-3.5 h-3.5" />
            </Button>

            {/* Edit mode configuration and removal actions */}
            {isEditMode && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    type="button"
                    aria-label={t('configureGadget')}
                    className="min-h-11 min-w-11 rounded-md text-muted-foreground hover:text-foreground hover:bg-primary/10 transition-colors"
                  >
                    <MoreVertical className="w-3.5 h-3.5" />
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
          </div>

          {/* One fixed status row prevents streaming counts and badges from changing grid height. */}
          <div className="flex h-5 min-w-0 items-center gap-1.5 overflow-x-auto whitespace-nowrap">
            {unreadCount > 0 && <span className="inline-flex shrink-0 items-center rounded-full bg-primary px-1.5 text-[11px] font-semibold tracking-tight text-primary-foreground" title={`${unreadCount} unread items`}>{t('unreadBadge', { count: unreadCount })}</span>}
            {ruleMatch?.matched && <span className="inline-flex shrink-0 items-center gap-1 rounded border border-amber-500/30 bg-amber-500/15 px-1.5 text-[11px] font-medium text-amber-700 dark:text-amber-300" title={ruleMatch.reason || 'Highlight rule matched'}><AlertTriangle className="w-3 h-3" /><span>{t('ruleMatchBadge')}</span></span>}
            {effectiveSeverity && <span className={`inline-flex shrink-0 items-center rounded border px-1.5 text-[10px] font-bold uppercase tracking-wider ${effectiveSeverity === 'critical' ? 'bg-destructive/15 text-destructive border-destructive/30' : effectiveSeverity === 'warning' ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30' : 'bg-blue-500/15 text-blue-700 dark:text-blue-300 border-blue-500/30'}`}>{effectiveSeverity === 'critical' ? t('severityCritical') : effectiveSeverity === 'warning' ? t('severityWarning') : t('severityInfo')}</span>}
            {definition.warnings && definition.warnings.length > 0 && <span className="inline-flex shrink-0 items-center gap-0.5 rounded border border-border bg-muted/40 px-1.5 text-[10px] font-medium text-muted-foreground" title="Setup required for this renderer"><Info className="w-3 h-3" /><span>{t('setupRequired')}</span></span>}
          </div>
        </header>

        <div ref={inlineScrollRef} className="min-h-0 flex-1 overflow-y-auto overscroll-contain" style={{ minHeight: bodyFloor }}>
        {/* Queue controls scroll inside this fixed-floor body and never change the grid geometry. */}
        {readingQueueCount > 0 && onApplyReadingQueue && (
          <div className="px-3 py-1.5 bg-primary/10 border-b border-primary/20 text-primary text-xs flex items-center justify-between">
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

        <div ref={inlineHostRef} className="min-h-full min-w-0 overflow-x-auto" />
        </div>
      </div>

      {/* Expanded Modal View (preserves filters, scroll position and local state) */}
      <Dialog open={isExpanded} onOpenChange={setIsExpanded}>
        <DialogContent onCloseAutoFocus={(event) => { event.preventDefault(); expandTriggerRef.current?.focus(); }} className="max-w-5xl w-[calc(100vw-2rem)] h-[calc(100dvh-2rem)] max-h-[calc(100dvh-2rem)] flex flex-col p-0 gap-0 overflow-hidden" style={{ marginTop: 'env(safe-area-inset-top)', marginBottom: 'env(safe-area-inset-bottom)', marginLeft: 'env(safe-area-inset-left)', marginRight: 'env(safe-area-inset-right)' }}>
          <DialogHeader className="shrink-0 p-4 border-b border-border flex flex-row items-center justify-between">
            <div className="flex items-center gap-2">
              <DialogTitle className="text-lg font-bold">{displayTitle}</DialogTitle>
              <span className="text-xs px-2 py-0.5 rounded bg-muted/40 font-mono text-muted-foreground">
                {definition.renderer}
              </span>
            </div>
            <DialogDescription className="sr-only">
              {t('gadgetExpandedDescription', { title: displayTitle })}
            </DialogDescription>
            <Button type="button" className="min-h-11 min-w-11" aria-label={t('close')} onClick={() => setIsExpanded(false)}>
              <X className="h-4 w-4" />
            </Button>
          </DialogHeader>
          <div ref={attachExpandedHost} className="min-h-0 flex-1 overflow-auto overscroll-contain p-4" />
        </DialogContent>
      </Dialog>
      {rendererHost && createPortal(renderGadgetContent(), rendererHost)}
    </>
  );
}
