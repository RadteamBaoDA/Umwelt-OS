'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, CheckCircle2, ChevronRight, Eye, LayoutTemplate, Sparkles, X } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { useCallback, useState } from 'react';
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
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import {
  applyDashboardPreset,
  dashboardKeys,
  listDashboardPresets,
  previewDashboardPreset,
  type Dashboard,
  type DashboardPreset,
  type DashboardWarning,
  type PresetPreview,
} from './api';

/** Props configuring the preset picker dialog and its target context. */
export interface PresetPickerProps {
  /** Controls dialog open state. */
  open: boolean;
  /** Callback fired when dialog open state changes. */
  onOpenChange: (open: boolean) => void;
  /** Currently active dashboard identifier when replacing or previewing against it. */
  currentDashboardId?: string | null;
  /** Currently active dashboard revision for atomic conflict detection on replace. */
  currentDashboardRevision?: number | null;
  /** Name of the active dashboard used for replace confirmation. */
  currentDashboardName?: string | null;
  /** Callback invoked when a preset has been successfully applied to create or update a dashboard. */
  onPresetApplied: (dashboard: Dashboard) => void;
}

/**
 * Modal dialog that allows owners to browse, preview, and apply curated dashboard presets.
 * Supports non-destructive preview with capability/access warnings and explicit replace confirmation.
 *
 * @param props Configuration and callbacks for the preset picker modal.
 * @returns Radix Dialog element containing the preset preview and application workflow.
 */
export function PresetPicker({
  open,
  onOpenChange,
  currentDashboardId,
  currentDashboardRevision,
  onPresetApplied,
}: PresetPickerProps) {
  const t = useTranslations('dashboard');
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();

  const [selectedPresetId, setSelectedPresetId] = useState<string | null>(null);
  const [applyMode, setApplyMode] = useState<'create' | 'replace'>('create');
  const [newDashboardName, setNewDashboardName] = useState<string>('');
  const [replaceConfirmed, setReplaceConfirmed] = useState<boolean>(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);

  // Fetch standard presets catalog
  const presetsQuery = useQuery({
    queryKey: dashboardKeys.presets,
    queryFn: ({ signal }) => listDashboardPresets(signal),
    enabled: open,
  });

  const presets = presetsQuery.data ?? [];
  const selectedPreset = presets.find((preset) => preset.id === selectedPresetId) ?? null;

  // Preview query mutation
  const previewMutation = useMutation({
    mutationFn: async (preset: DashboardPreset) => {
      setPreviewError(null);
      return previewDashboardPreset(
        preset.id,
        {
          target_dashboard_id: applyMode === 'replace' ? currentDashboardId : null,
        },
        session.csrfToken,
      );
    },
    onError: (error) => {
      setPreviewError(error instanceof Error ? error.message : t('previewingPreset'));
    },
  });

  // Apply mutation
  const applyMutation = useMutation({
    mutationFn: async (params: { presetId: string; preview: PresetPreview }) => {
      setApplyError(null);
      return applyDashboardPreset(
        params.presetId,
        {
          preview_fingerprint: params.preview.preview_fingerprint,
          mode: applyMode,
          name: applyMode === 'create' ? newDashboardName.trim() || params.preview.name : null,
          target_dashboard_id: applyMode === 'replace' ? currentDashboardId : null,
          expected_revision: applyMode === 'replace' ? currentDashboardRevision : null,
          replace_confirmed: applyMode === 'replace' ? replaceConfirmed : undefined,
        },
        session.csrfToken,
      );
    },
    onSuccess: (updatedDashboard) => {
      void queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
      if (updatedDashboard.id) {
        void queryClient.invalidateQueries({ queryKey: dashboardKeys.detail(updatedDashboard.id) });
      }
      onPresetApplied(updatedDashboard);
      handleClose();
    },
    onError: (error) => {
      setApplyError(error instanceof Error ? error.message : t('saveFailed'));
    },
  });

  /** Closes dialog and resets draft preview selection state. */
  const handleClose = useCallback(() => {
    setSelectedPresetId(null);
    setPreviewError(null);
    setApplyError(null);
    setReplaceConfirmed(false);
    previewMutation.reset();
    applyMutation.reset();
    onOpenChange(false);
  }, [applyMutation, onOpenChange, previewMutation]);

  /** Selects a preset and triggers preview generation. */
  const handleSelectPreset = useCallback(
    (preset: DashboardPreset) => {
      setSelectedPresetId(preset.id);
      setNewDashboardName(preset.label);
      setPreviewError(null);
      setApplyError(null);
      void previewMutation.mutateAsync(preset);
    },
    [previewMutation],
  );

  /** Confirms and applies the active preview. */
  const handleApply = useCallback(() => {
    if (!selectedPreset || !previewMutation.data) return;
    if (applyMode === 'replace' && !replaceConfirmed) return;
    void applyMutation.mutateAsync({
      presetId: selectedPreset.id,
      preview: previewMutation.data,
    });
  }, [applyMode, applyMutation, previewMutation.data, replaceConfirmed, selectedPreset]);

  // Aggregate missing access / capability warnings across all slots
  const allWarnings: DashboardWarning[] =
    previewMutation.data?.slots.flatMap((slot) => slot.warnings) ?? [];

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="max-w-3xl max-h-[85vh] flex flex-col p-0 gap-0 overflow-hidden">
        <DialogHeader className="p-6 pb-4 border-b border-border">
          <div className="flex items-center gap-2 text-primary font-semibold text-sm tracking-wide uppercase">
            <LayoutTemplate className="w-4 h-4" />
            <span>{t('presetPickerTitle')}</span>
          </div>
          <DialogTitle className="text-xl font-bold">{t('presetPickerTitle')}</DialogTitle>
          <DialogDescription className="text-muted-foreground text-sm">
            {t('presetPickerDesc')}
          </DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto p-6 grid grid-cols-1 md:grid-cols-2 gap-6">
          {/* Preset list selection column */}
          <div className="space-y-3">
            <h3 className="text-sm font-semibold text-foreground flex items-center justify-between">
              <span>{t('selectPreset')}</span>
              {presetsQuery.isLoading && <span className="text-xs text-muted-foreground animate-pulse">Loading…</span>}
            </h3>

            <div className="space-y-2 max-h-[380px] overflow-y-auto pr-1">
              {presets.map((preset) => {
                const isSelected = preset.id === selectedPresetId;
                return (
                  <button
                    key={preset.id}
                    type="button"
                    onClick={() => handleSelectPreset(preset)}
                    className={`w-full text-left p-3.5 rounded-lg border transition-all flex items-start justify-between gap-3 ${
                      isSelected
                        ? 'border-primary bg-primary/10 shadow-sm'
                        : 'border-border bg-card hover:bg-primary/5'
                    }`}
                  >
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-foreground text-sm">{preset.label}</span>
                        <span className="text-[11px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-muted/40 text-muted-foreground font-mono">
                          {preset.family}
                        </span>
                      </div>
                      <p className="text-xs text-muted-foreground mt-1">
                        {t('presetSlotsCount', { count: preset.slots.length })}: {preset.slots.map((s) => s.renderer).join(', ')}
                      </p>
                    </div>
                    <ChevronRight
                      className={`w-4 h-4 mt-1 transition-transform ${
                        isSelected ? 'text-primary rotate-90' : 'text-muted-foreground'
                      }`}
                    />
                  </button>
                );
              })}
            </div>
          </div>

          {/* Preset preview and apply configuration column */}
          <div className="flex flex-col gap-4 border-l border-border md:pl-6">
            {!selectedPreset ? (
              <div className="flex-1 flex flex-col items-center justify-center p-8 text-center border border-dashed border-border rounded-lg text-muted-foreground">
                <Sparkles className="w-8 h-8 mb-2 opacity-50" />
                <p className="text-sm">{t('selectPreset')}</p>
              </div>
            ) : (
              <div className="space-y-4 flex-1">
                <div>
                  <h4 className="text-base font-bold text-foreground flex items-center gap-2">
                    <span>{selectedPreset.label}</span>
                    <span className="text-xs font-normal text-muted-foreground">
                      ({selectedPreset.slots.length} gadgets)
                    </span>
                  </h4>
                  {previewMutation.isPending && (
                    <p className="text-xs text-muted-foreground animate-pulse mt-1">
                      {t('previewingPreset')}
                    </p>
                  )}
                  {previewError && (
                    <div className="p-2.5 mt-2 rounded bg-destructive/10 text-destructive text-xs flex items-center gap-2">
                      <AlertCircle className="w-4 h-4 shrink-0" />
                      <span>{previewError}</span>
                    </div>
                  )}
                </div>

                {/* Resolved slots preview */}
                {previewMutation.data && (
                  <div className="space-y-3">
                    <div className="p-3 rounded-md bg-muted/30 border border-border text-xs space-y-1.5">
                      <div className="font-semibold text-foreground flex items-center gap-1.5">
                        <Eye className="w-3.5 h-3.5" />
                        <span>Preview Summary</span>
                      </div>
                      <div className="text-muted-foreground font-mono text-[11px] truncate">
                        {t('fingerprint', { fingerprint: previewMutation.data.preview_fingerprint.slice(0, 16) + '…' })}
                      </div>
                      <div className="grid grid-cols-2 gap-1 text-[11px] pt-1">
                        <span>{t('desktopLayout')}: 20 cols</span>
                        <span>{t('mobileLayout')}: stack</span>
                      </div>
                    </div>

                    {/* Per-gadget readiness: warnings come from the preview; nothing is connected on the owner's behalf */}
                    <div className="space-y-1.5">
                      <p className="text-xs font-semibold text-foreground">
                        {t('slotsReadiness', {
                          ready: previewMutation.data.slots.filter((slot) => slot.warnings.length === 0).length,
                          setup: previewMutation.data.slots.filter((slot) => slot.warnings.length > 0).length,
                        })}
                      </p>
                      <ul className="divide-y divide-border rounded-md border border-border">
                        {previewMutation.data.slots.map((slot) => (
                          <li key={slot.slot_id} className="flex items-center justify-between gap-2 px-2.5 py-1.5 text-xs">
                            <span className="min-w-0 truncate font-mono">{slot.renderer}</span>
                            {slot.warnings.length === 0 ? (
                              <span className="shrink-0 text-muted-foreground">{t('slotReady')}</span>
                            ) : (
                              <span className="flex shrink-0 items-center gap-2">
                                <span className="font-semibold text-destructive">{t('slotNeedsSetup')}</span>
                                <Link href="/settings/sources" className="inline-flex min-h-11 items-center text-primary underline underline-offset-4">{t('slotConfigure')}</Link>
                              </span>
                            )}
                          </li>
                        ))}
                      </ul>
                      <p className="text-[11px] text-muted-foreground">{t('presetsNoConnect')}</p>
                    </div>

                    {/* Missing access / capability warnings banner */}
                    {allWarnings.length > 0 && (
                      <div className="p-3 rounded-md bg-muted/30 border border-border text-foreground text-xs space-y-1.5">
                        <div className="font-semibold flex items-center gap-1.5">
                          <AlertCircle className="w-4 h-4 shrink-0" />
                          <span>{t('missingAccessWarnings')} ({allWarnings.length})</span>
                        </div>
                        <ul className="list-disc list-inside space-y-0.5 text-[11px] pl-1">
                          {allWarnings.slice(0, 5).map((warning, idx) => (
                            <li key={idx}>
                              {warning.capability ? `Capability "${warning.capability}"` : warning.code}
                              {warning.setup_group ? ` (group: ${warning.setup_group})` : ''}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Mode selection: Create vs Replace */}
                    <div className="space-y-2 pt-2 border-t border-border">
                      <Label className="text-xs font-semibold">{t('applyMode')}</Label>
                      <div className="grid grid-cols-2 gap-2">
                        <button
                          type="button"
                          onClick={() => setApplyMode('create')}
                          className={`p-2.5 text-xs font-medium rounded-md border text-center transition-colors ${
                            applyMode === 'create'
                              ? 'border-primary bg-primary/10 font-bold text-foreground'
                              : 'border-border text-muted-foreground hover:bg-primary/5'
                          }`}
                        >
                          {t('applyModeCreate')}
                        </button>
                        <button
                          type="button"
                          disabled={!currentDashboardId}
                          onClick={() => setApplyMode('replace')}
                          className={`p-2.5 text-xs font-medium rounded-md border text-center transition-colors ${
                            applyMode === 'replace'
                              ? 'border-destructive bg-destructive/10 font-bold text-destructive'
                              : 'border-border text-muted-foreground hover:bg-primary/5 disabled:opacity-40'
                          }`}
                        >
                          {t('applyModeReplace')}
                        </button>
                      </div>

                      {applyMode === 'create' ? (
                        <div className="space-y-1 pt-1">
                          <Label htmlFor="preset-new-name" className="text-xs">
                            {t('dashboardName')}
                          </Label>
                          <Input
                            id="preset-new-name"
                            value={newDashboardName}
                            onChange={(e) => setNewDashboardName(e.target.value)}
                            placeholder={selectedPreset.label}
                            className="h-8 text-xs"
                          />
                        </div>
                      ) : (
                        <div className="p-2.5 rounded bg-destructive/10 border border-destructive/20 text-xs space-y-2 mt-2">
                          <p className="text-destructive font-medium leading-relaxed">
                            {t('confirmReplaceWarning')}
                          </p>
                          <div className="flex items-center space-x-2 pt-1">
                            <Checkbox
                              id="confirm-replace-cb"
                              checked={replaceConfirmed}
                              onCheckedChange={(checked) => setReplaceConfirmed(Boolean(checked))}
                            />
                            <label
                              htmlFor="confirm-replace-cb"
                              className="text-[11px] font-medium leading-none cursor-pointer text-foreground"
                            >
                              {t('confirmReplaceCheckbox')}
                            </label>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        {applyError && (
          <div className="px-6 py-2.5 bg-destructive/10 border-t border-destructive/20 text-destructive text-xs flex items-center justify-between">
            <div className="flex items-center gap-2">
              <AlertCircle className="w-4 h-4 shrink-0" />
              <span>{applyError}</span>
            </div>
            <button
              type="button"
              onClick={() => setApplyError(null)}
              className="text-xs underline hover:no-underline"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )}

        <DialogFooter className="p-4 border-t border-border flex items-center justify-between gap-2 bg-muted/10 sm:justify-between">
          <Button type="button" className="secondary" onClick={handleClose}>
            {t('cancel')}
          </Button>

          <Button
            type="button"
            disabled={
              !selectedPreset ||
              !previewMutation.data ||
              previewMutation.isPending ||
              applyMutation.isPending ||
              (applyMode === 'replace' && !replaceConfirmed)
            }
            onClick={handleApply}
            className={`font-semibold ${
              applyMode === 'replace' ? 'bg-destructive text-destructive-foreground' : ''
            }`}
          >
            {applyMutation.isPending ? (
              t('applyingPreset')
            ) : (
              <span className="flex items-center gap-1.5">
                <CheckCircle2 className="w-4 h-4" />
                <span>{t('applyPreset')}</span>
              </span>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
