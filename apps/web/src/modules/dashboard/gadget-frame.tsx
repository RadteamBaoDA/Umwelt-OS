'use client';

import {
  AlertTriangle,
  ArrowDownCircle,
  ExternalLink,
  GripHorizontal,
  Info,
  Maximize2,
  Minimize2,
  MoreVertical,
  Settings,
  Trash2,
  X,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useCallback, useId, useState } from 'react';
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
import type { GadgetInstance, HighlightRule } from './api';

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
}

/**
 * Standard container card wrapping each dashboard gadget.
 * Owns header actions, unread/rule/severity badges with visible text,
 * expand modal without layout mutation, and reading queue banners to prevent jumping.
 *
 * @param props Gadget configuration, edit mode flags, and interactive callbacks.
 * @returns Accessible gadget container element.
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
  children,
}: GadgetFrameProps) {
  const t = useTranslations('dashboard');
  const [isExpanded, setIsExpanded] = useState<boolean>(false);
  const titleId = useId();

  const definition = instance.definition;
  const displayTitle = instance.title || definition.name || definition.renderer;

  // Derive most severe highlight rule or rule match
  const effectiveSeverity = ruleMatch?.severity || definition.highlight_rules?.[0]?.severity;

  /** Toggles expanded fullscreen modal view without modifying underlying grid layout. */
  const toggleExpanded = useCallback(() => {
    setIsExpanded((prev) => !prev);
  }, []);

  /** Renders the inner content of the gadget (either custom children or planned renderer card). */
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
        tabIndex={isEditMode ? 0 : undefined}
        onClick={isEditMode ? onSelect : undefined}
        className={`group relative flex flex-col h-full w-full rounded-xl border bg-card text-card-foreground shadow-xs transition-shadow overflow-hidden ${
          isSelected ? 'ring-2 ring-primary border-primary' : 'border-border'
        } ${isDragging ? 'opacity-40 shadow-lg scale-[0.99]' : ''} ${
          isResizing ? 'ring-1 ring-primary/60' : ''
        }`}
      >
        {/* Gadget Header */}
        <header
          className={`flex items-center justify-between gap-2 px-3.5 py-2.5 border-b border-border select-none ${
            isEditMode
              ? 'cursor-grab active:cursor-grabbing bg-muted/20 hover:bg-muted/30'
              : 'bg-card'
          }`}
          onPointerDown={isEditMode ? onDragHandlePointerDown : undefined}
        >
          <div className="flex items-center gap-2 min-w-0 flex-1">
            {isEditMode && (
              <GripHorizontal className="w-4 h-4 text-muted-foreground shrink-0 opacity-70 group-hover:opacity-100" />
            )}

            <h3
              id={titleId}
              className="font-semibold text-sm truncate text-foreground leading-tight"
              title={displayTitle}
            >
              {displayTitle}
            </h3>

            {/* Badges: Unread, Rule match, Severity - always with text, not color only */}
            <div className="flex items-center gap-1.5 shrink-0 flex-wrap">
              {unreadCount > 0 && (
                <span
                  className="inline-flex items-center px-1.5 py-0.2 rounded-full text-[11px] font-semibold bg-primary text-primary-foreground tracking-tight"
                  title={`${unreadCount} unread items`}
                >
                  {t('unreadBadge', { count: unreadCount })}
                </span>
              )}

              {ruleMatch?.matched && (
                <span
                  className="inline-flex items-center gap-1 px-1.5 py-0.2 rounded text-[11px] font-medium bg-amber-500/15 text-amber-700 dark:text-amber-300 border border-amber-500/30"
                  title={ruleMatch.reason || 'Highlight rule matched'}
                >
                  <AlertTriangle className="w-3 h-3" />
                  <span>{t('ruleMatchBadge')}</span>
                </span>
              )}

              {effectiveSeverity && (
                <span
                  className={`inline-flex items-center px-1.5 py-0.2 rounded text-[10px] font-bold uppercase tracking-wider ${
                    effectiveSeverity === 'critical'
                      ? 'bg-destructive/15 text-destructive border border-destructive/30'
                      : effectiveSeverity === 'warning'
                      ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border border-amber-500/30'
                      : 'bg-blue-500/15 text-blue-700 dark:text-blue-300 border border-blue-500/30'
                  }`}
                >
                  {effectiveSeverity === 'critical'
                    ? t('severityCritical')
                    : effectiveSeverity === 'warning'
                    ? t('severityWarning')
                    : t('severityInfo')}
                </span>
              )}

              {definition.warnings && definition.warnings.length > 0 && (
                <span
                  className="inline-flex items-center gap-0.5 px-1.5 py-0.2 rounded text-[10px] font-medium bg-muted/40 text-muted-foreground border border-border"
                  title="Setup required for this renderer"
                >
                  <Info className="w-3 h-3" />
                  <span>{t('setupRequired')}</span>
                </span>
              )}
            </div>
          </div>

          {/* Action buttons */}
          <div className="flex items-center gap-1 shrink-0">
            {/* Reading queue indicator button if queued updates exist */}
            {readingQueueCount > 0 && onApplyReadingQueue && (
              <button
                type="button"
                onClick={onApplyReadingQueue}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-semibold bg-primary/20 text-primary hover:bg-primary/30 transition-colors animate-pulse"
                title={t('readingUpdatesQueued', { count: readingQueueCount })}
              >
                <ArrowDownCircle className="w-3.5 h-3.5" />
                <span>{readingQueueCount}</span>
              </button>
            )}

            {/* Expand / Maximize modal trigger */}
            <button
              type="button"
              onClick={toggleExpanded}
              aria-label={t('expand')}
              className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-primary/10 transition-colors"
            >
              <Maximize2 className="w-3.5 h-3.5" />
            </button>

            {/* Edit mode configuration and removal actions */}
            {isEditMode && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    aria-label={t('configureGadget')}
                    className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-primary/10 transition-colors"
                  >
                    <MoreVertical className="w-3.5 h-3.5" />
                  </button>
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

        {/* Queued reading updates notification banner inside gadget */}
        {readingQueueCount > 0 && onApplyReadingQueue && (
          <div className="px-3 py-1.5 bg-primary/10 border-b border-primary/20 text-primary text-xs flex items-center justify-between">
            <span className="font-medium">
              {t('readingUpdatesQueued', { count: readingQueueCount })}
            </span>
            <button
              type="button"
              onClick={onApplyReadingQueue}
              className="font-bold underline hover:no-underline text-xs"
            >
              {t('showUpdates')}
            </button>
          </div>
        )}

        {/* Scrollable Content Body with reading stability */}
        <div className="flex-1 overflow-y-auto overscroll-contain min-h-0">
          {renderGadgetContent()}
        </div>
      </div>

      {/* Expanded Modal View (preserves filters, scroll position and local state) */}
      <Dialog open={isExpanded} onOpenChange={setIsExpanded}>
        <DialogContent className="max-w-5xl h-[85vh] flex flex-col p-0 gap-0 overflow-hidden">
          <DialogHeader className="p-4 border-b border-border flex flex-row items-center justify-between">
            <div className="flex items-center gap-2">
              <DialogTitle className="text-lg font-bold">{displayTitle}</DialogTitle>
              <span className="text-xs px-2 py-0.5 rounded bg-muted/40 font-mono text-muted-foreground">
                {definition.renderer}
              </span>
            </div>
            <DialogDescription className="sr-only">
              Expanded gadget view preserving local filters and state
            </DialogDescription>
          </DialogHeader>
          <div className="flex-1 overflow-y-auto p-4">{renderGadgetContent()}</div>
        </DialogContent>
      </Dialog>
    </>
  );
}
