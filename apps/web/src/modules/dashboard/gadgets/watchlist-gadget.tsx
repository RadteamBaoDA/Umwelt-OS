'use client';

import {
  AlertCircle,
  AlertTriangle,
  Bell,
  Eye,
  Plus,
  RotateCw,
  Search,
  ShieldAlert,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import type { GadgetInstance, HighlightRule } from '../api';

/** Single monitored rule or target symbol entry on the watchlist table. */
export interface WatchlistItem {
  id: string;
  target: string;
  targetType: 'symbol' | 'keyword' | 'entity';
  condition: string;
  status: 'matched' | 'monitoring' | 'alert';
  severity: 'info' | 'warning' | 'critical';
  lastMatchedAt: string | null;
  matchReason?: string;
  matchCount: number;
}

/** Props for the WatchlistGadget component. */
export interface WatchlistGadgetProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
  /** Optional initial watchlist entries. */
  initialItems?: WatchlistItem[];
  /** Callback fired when user wants to add or adjust a rule. */
  onAddRule?: () => void;
}

/**
 * Standard Watchlist and Rules table gadget template.
 * Renders tracked symbols, keywords, condition matches, and explainable highlight rules
 * with visible text-based severity badges (Info, Warning, Critical) and notification states.
 *
 * @param props Gadget instance configuration and watchlist items.
 * @returns Accessible watchlist table gadget component.
 */
export function WatchlistGadget({
  instance,
  initialItems = [],
  onAddRule,
}: WatchlistGadgetProps) {
  const t = useTranslations('dashboard');
  const daily = useTranslations('daily');
  const display = useDisplayPreferences();

  const [items, setItems] = useState<WatchlistItem[]>(initialItems);
  const [filterQuery, setFilterQuery] = useState<string>('');

  const definition = instance.definition;
  const highlightRules: HighlightRule[] = definition.highlight_rules ?? [];

  // Filter items by search query
  const filteredItems = items.filter((item) => {
    if (!filterQuery) return true;
    const q = filterQuery.toLowerCase();
    return (
      item.target.toLowerCase().includes(q) ||
      item.condition.toLowerCase().includes(q) ||
      item.matchReason?.toLowerCase().includes(q)
    );
  });

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-hidden">
      {/* Header bar */}
      <div className="flex items-center justify-between border-b border-border pb-2 text-xs">
        <div className="flex items-center gap-1.5 font-medium text-muted-foreground">
          <Eye className="w-3.5 h-3.5 text-primary" />
          <span>
            {daily('monitoredRules', { count: items.length })}
          </span>
        </div>

        <div className="flex items-center gap-1">
          {highlightRules.length > 0 && (
            <span
              className="px-1.5 py-0.2 rounded text-[10px] font-semibold bg-muted/30 text-muted-foreground"
              title={daily('highlightRulesApplied', { count: highlightRules.length })}
            >
              {daily('ruleCount', { count: highlightRules.length })}
            </span>
          )}
          {onAddRule && (
            <Button
              type="button"
              onClick={onAddRule}
              className="h-6 px-2 text-[11px] font-semibold gap-1"
            >
              <Plus className="w-3 h-3" />
              <span>{daily('addRule')}</span>
            </Button>
          )}
        </div>
      </div>

      {/* Empty State */}
      {filteredItems.length === 0 && (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center text-muted-foreground">
          <ShieldAlert className="w-8 h-8 mb-2 opacity-50" />
          <p className="text-xs font-semibold text-foreground mb-1">{daily('watchlistEmpty')}</p>
          <p className="text-[11px] max-w-xs text-muted-foreground">
            {daily('watchlistEmptyDetail')}
          </p>
        </div>
      )}

      {/* Watchlist Table */}
      {filteredItems.length > 0 && (
        <div className="flex-1 overflow-y-auto space-y-2 min-h-0 pr-0.5">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="border-b border-border text-[11px] text-muted-foreground font-medium">
                <th scope="col" className="pb-1.5 pl-1 font-semibold">
                  Target
                </th>
                <th scope="col" className="pb-1.5 font-semibold">
                  Condition / Trigger
                </th>
                <th scope="col" className="pb-1.5 text-right pr-1 font-semibold">
                  Status & Severity
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/50">
              {filteredItems.map((item) => {
                const lastMatched = item.lastMatchedAt
                  ? formatDateTime(item.lastMatchedAt, display.locale, display.timezone)
                  : 'Never';

                return (
                  <tr key={item.id} className="hover:bg-muted/15 transition-colors group">
                    {/* Target column */}
                    <td className="py-2 pl-1 align-top">
                      <div className="font-semibold text-foreground">{item.target}</div>
                      <div className="text-[10px] text-muted-foreground font-mono uppercase tracking-wider">
                        {item.targetType}
                      </div>
                    </td>

                    {/* Condition & match reason column */}
                    <td className="py-2 pr-2 align-top">
                      <div className="text-foreground/90 font-medium leading-tight">
                        {item.condition}
                      </div>
                      {item.matchReason && (
                        <div className="text-[11px] text-muted-foreground line-clamp-1 mt-0.5">
                          {item.matchReason}
                        </div>
                      )}
                      <div className="text-[10px] text-muted-foreground/70 font-mono mt-0.5">
                        Matched: {lastMatched} ({item.matchCount}x)
                      </div>
                    </td>

                    {/* Status & severity column */}
                    <td className="py-2 pr-1 align-top text-right space-y-1">
                      {/* Visible text severity badge */}
                      <div>
                        <span
                          className={`inline-flex items-center px-1.5 py-0.2 rounded text-[10px] font-bold uppercase tracking-wider ${
                            item.severity === 'critical'
                              ? 'bg-destructive/15 text-destructive border border-destructive/30'
                              : item.severity === 'warning'
                              ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border border-amber-500/30'
                              : 'bg-blue-500/15 text-blue-700 dark:text-blue-300 border border-blue-500/30'
                          }`}
                        >
                          {item.severity === 'critical' ? (
                            <AlertCircle className="w-2.5 h-2.5 mr-0.5" />
                          ) : item.severity === 'warning' ? (
                            <AlertTriangle className="w-2.5 h-2.5 mr-0.5" />
                          ) : null}
                          {item.severity}
                        </span>
                      </div>

                      {/* State badge */}
                      <div>
                        <span
                          className={`inline-flex items-center px-1.5 py-0.2 rounded text-[10px] font-medium font-mono ${
                            item.status === 'alert'
                              ? 'text-destructive font-bold'
                              : item.status === 'matched'
                              ? 'text-amber-600 dark:text-amber-400 font-bold'
                              : 'text-muted-foreground'
                          }`}
                        >
                          {item.status.toUpperCase()}
                        </span>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
