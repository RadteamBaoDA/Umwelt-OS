'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  AlertCircle,
  Bell,
  Check,
  Plus,
  Settings,
  Trash2,
  X,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import {
  dashboardKeys,
  patchGadgetDefinition,
  patchGadgetInstance,
  type GadgetInstance,
  type HighlightRule,
} from './api';

/** Props for the GadgetSettings modal dialog. */
export interface GadgetSettingsProps {
  /** Controls open state of the settings modal. */
  open: boolean;
  /** Callback fired when open state changes. */
  onOpenChange: (open: boolean) => void;
  /** Target gadget instance to inspect and edit. */
  instance: GadgetInstance | null;
  /** ID of the dashboard hosting this gadget instance. */
  dashboardId: string;
  /** Shared revision of the dashboard for instance updates. */
  dashboardRevision: number;
  /** Optional callback fired when gadget settings are saved successfully. */
  onSaved?: () => void;
}

/**
 * Modal dialog for configuring an individual gadget instance and its reusable definition.
 * Allows editing gadget title, max items limit, filter keywords, scope selectors,
 * and rule-based highlight triggers with severity and notification settings.
 *
 * @param props Configuration and state handlers for the settings dialog.
 * @returns Radix Dialog element.
 */
export function GadgetSettings({
  open,
  onOpenChange,
  instance,
  dashboardId,
  dashboardRevision,
  onSaved,
}: GadgetSettingsProps) {
  const t = useTranslations('dashboard');
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();

  // Form states initialized from instance & definition
  const [title, setTitle] = useState<string>('');
  const [limit, setLimit] = useState<number>(20);
  const [keywords, setKeywords] = useState<string>('');
  const [excludeKeywords, setExcludeKeywords] = useState<string>('');
  const [symbols, setSymbols] = useState<string>('');
  const [highlightRules, setHighlightRules] = useState<HighlightRule[]>([]);
  const [saveError, setSaveError] = useState<string | null>(null);

  // New highlight rule subform state
  const [newRuleKeywords, setNewRuleKeywords] = useState<string>('');
  const [newRuleSeverity, setNewRuleSeverity] = useState<'info' | 'warning' | 'critical'>('warning');
  const [newRuleNotify, setNewRuleNotify] = useState<boolean>(true);

  // Sync state when instance opens
  useEffect(() => {
    if (instance) {
      setTitle(instance.title || '');
      const def = instance.definition;
      setLimit(def.filters?.limit ?? 20);
      setKeywords((def.filters?.keywords ?? []).join(', '));
      setExcludeKeywords((def.filters?.exclude_keywords ?? []).join(', '));
      setSymbols((def.scope?.symbols ?? []).join(', '));
      setHighlightRules(def.highlight_rules ? [...def.highlight_rules] : []);
      setSaveError(null);
    }
  }, [instance, open]);

  // Mutation saving both instance title and reusable definition configuration
  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!instance) return;
      setSaveError(null);

      // Parse comma-separated lists
      const parsedKeywords = keywords
        .split(',')
        .map((k) => k.trim())
        .filter(Boolean);
      const parsedExcludeKeywords = excludeKeywords
        .split(',')
        .map((k) => k.trim())
        .filter(Boolean);
      const parsedSymbols = symbols
        .split(',')
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);

      // 1. Update instance title if changed
      if (title.trim() !== (instance.title || '')) {
        await patchGadgetInstance(
          dashboardId,
          instance.id,
          dashboardRevision,
          { title: title.trim() || null },
          session.csrfToken,
        );
      }

      // 2. Update reusable definition filters, scope, and highlight rules
      await patchGadgetDefinition(
        instance.definition_id,
        {
          expected_revision: instance.definition.revision,
          filters: {
            limit: Math.max(1, Math.min(100, limit)),
            keywords: parsedKeywords.length > 0 ? parsedKeywords : undefined,
            exclude_keywords: parsedExcludeKeywords.length > 0 ? parsedExcludeKeywords : undefined,
          },
          scope: {
            ...instance.definition.scope,
            symbols: parsedSymbols.length > 0 ? parsedSymbols : undefined,
          },
          highlight_rules: highlightRules,
        },
        session.csrfToken,
      );
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: dashboardKeys.detail(dashboardId) });
      void queryClient.invalidateQueries({ queryKey: dashboardKeys.definitions });
      onOpenChange(false);
      onSaved?.();
    },
    onError: (err) => {
      setSaveError(err instanceof Error ? err.message : 'Failed to save gadget configuration');
    },
  });

  /** Adds a new highlight rule to the local state. */
  const handleAddHighlightRule = () => {
    const parsed = newRuleKeywords
      .split(',')
      .map((k) => k.trim())
      .filter(Boolean);
    if (parsed.length === 0) return;

    const rule: HighlightRule = {
      id: `rule-${Date.now()}`,
      keywords: parsed,
      severity: newRuleSeverity,
      notify: newRuleNotify,
    };

    setHighlightRules((prev) => [...prev, rule]);
    setNewRuleKeywords('');
  };

  /** Removes a highlight rule from the local state. */
  const handleRemoveHighlightRule = (ruleId: string) => {
    setHighlightRules((prev) => prev.filter((r) => r.id !== ruleId));
  };

  if (!instance) return null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl max-h-[85vh] flex flex-col p-0 gap-0 overflow-hidden">
        <DialogHeader className="p-4 border-b border-border">
          <div className="flex items-center gap-2">
            <Settings className="w-4 h-4 text-primary" />
            <DialogTitle className="text-base font-bold">
              {t('configureGadgetTitle')}
            </DialogTitle>
          </div>
          <DialogDescription className="text-xs text-muted-foreground">
            Customize display title, filter queries, limits, and highlight rules for this {instance.definition.renderer} gadget.
          </DialogDescription>
        </DialogHeader>

        {/* Scrollable Form Body */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {saveError && (
            <div className="p-2.5 rounded-lg bg-destructive/15 border border-destructive/30 text-destructive text-xs flex items-center gap-2">
              <AlertCircle className="w-4 h-4 shrink-0" />
              <span>{saveError}</span>
            </div>
          )}

          {/* Title input */}
          <div className="space-y-1.5">
            <Label htmlFor="gadget-title" className="text-xs font-semibold">
              {t('instanceTitle')}
            </Label>
            <Input
              id="gadget-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder={instance.definition.name || instance.definition.renderer}
              className="h-8 text-xs bg-muted/20"
            />
          </div>

          {/* Limit & Scope row */}
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="gadget-limit" className="text-xs font-semibold">
                Max items limit
              </Label>
              <Input
                id="gadget-limit"
                type="number"
                min={1}
                max={100}
                value={limit}
                onChange={(e) => setLimit(parseInt(e.target.value, 10) || 20)}
                className="h-8 text-xs font-mono bg-muted/20"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="gadget-symbols" className="text-xs font-semibold">
                Tracked symbols (comma-separated)
              </Label>
              <Input
                id="gadget-symbols"
                value={symbols}
                onChange={(e) => setSymbols(e.target.value)}
                placeholder="NVDA, TSLA, BTC"
                className="h-8 text-xs font-mono uppercase bg-muted/20"
              />
            </div>
          </div>

          {/* Keywords filter */}
          <div className="space-y-1.5">
            <Label htmlFor="gadget-keywords" className="text-xs font-semibold">
              Filter keywords (include)
            </Label>
            <Input
              id="gadget-keywords"
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
              placeholder="AI, semiconductor, architecture"
              className="h-8 text-xs bg-muted/20"
            />
          </div>

          {/* Exclude keywords */}
          <div className="space-y-1.5">
            <Label htmlFor="gadget-exclude-keywords" className="text-xs font-semibold">
              Exclude keywords
            </Label>
            <Input
              id="gadget-exclude-keywords"
              value={excludeKeywords}
              onChange={(e) => setExcludeKeywords(e.target.value)}
              placeholder="spam, promotion"
              className="h-8 text-xs bg-muted/20"
            />
          </div>

          {/* Highlight Rules Section */}
          <div className="space-y-2.5 pt-2 border-t border-border">
            <div className="flex items-center justify-between">
              <Label className="text-xs font-bold text-foreground">
                Highlight Rules ({highlightRules.length})
              </Label>
              <span className="text-[10px] text-muted-foreground font-mono">
                Explainable trigger conditions
              </span>
            </div>

            {/* List existing rules */}
            {highlightRules.length > 0 && (
              <div className="space-y-1.5">
                {highlightRules.map((rule) => (
                  <div
                    key={rule.id}
                    className="flex items-center justify-between p-2 rounded-lg border border-border bg-muted/10 text-xs"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className={`px-1.5 py-0.2 rounded text-[10px] font-bold uppercase ${
                          rule.severity === 'critical'
                            ? 'bg-destructive/15 text-destructive'
                            : rule.severity === 'warning'
                            ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300'
                            : 'bg-blue-500/15 text-blue-700 dark:text-blue-300'
                        }`}
                      >
                        {rule.severity}
                      </span>
                      <span className="font-medium text-foreground truncate">
                        {rule.keywords.join(', ')}
                      </span>
                      {rule.notify && (
                        <Bell className="w-3 h-3 text-muted-foreground shrink-0" />
                      )}
                    </div>

                    <button
                      type="button"
                      onClick={() => handleRemoveHighlightRule(rule.id)}
                      className="p-1 rounded text-muted-foreground hover:text-destructive transition-colors"
                      title="Remove highlight rule"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}

            {/* Add rule form */}
            <div className="p-2.5 rounded-lg border border-border/80 bg-muted/15 space-y-2">
              <span className="text-[11px] font-semibold text-muted-foreground">
                Add new highlight rule
              </span>
              <div className="flex items-center gap-2">
                <Input
                  value={newRuleKeywords}
                  onChange={(e) => setNewRuleKeywords(e.target.value)}
                  placeholder="Keywords (e.g. breach, outage)"
                  className="h-7 text-xs flex-1 bg-background"
                />

                <Select
                  value={newRuleSeverity}
                  onValueChange={(val) =>
                    setNewRuleSeverity(val as 'info' | 'warning' | 'critical')
                  }
                >
                  <SelectTrigger className="h-7 w-28 text-xs bg-background">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="info">Info</SelectItem>
                    <SelectItem value="warning">Warning</SelectItem>
                    <SelectItem value="critical">Critical</SelectItem>
                  </SelectContent>
                </Select>

                <Button
                  type="button"
                  onClick={handleAddHighlightRule}
                  disabled={!newRuleKeywords.trim()}
                  className="h-7 px-2 text-xs font-semibold shrink-0"
                >
                  <Plus className="w-3 h-3 mr-0.5" />
                  <span>Add</span>
                </Button>
              </div>

              <div className="flex items-center gap-2 pt-0.5">
                <Checkbox
                  id="notify-rule"
                  checked={newRuleNotify}
                  onCheckedChange={(c) => setNewRuleNotify(!!c)}
                />
                <Label htmlFor="notify-rule" className="text-[11px] font-normal cursor-pointer">
                  Notify on rule match
                </Label>
              </div>
            </div>
          </div>
        </div>

        {/* Footer actions */}
        <DialogFooter className="p-3 border-t border-border bg-muted/10 gap-2">
          <Button
            type="button"
            onClick={() => onOpenChange(false)}
            disabled={saveMutation.isPending}
            className="secondary text-xs h-8"
          >
            {t('cancelEdit')}
          </Button>
          <Button
            type="button"
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending}
            className="text-xs h-8 font-semibold"
          >
            {saveMutation.isPending ? t('saving') : t('saveLayout')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
