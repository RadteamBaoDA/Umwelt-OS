'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, CheckCircle2, ChevronRight, LayoutTemplate, Sparkles, X } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { DataShape } from '@/core/data-shapes';
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
import { ApiError } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import {
  applyDashboardPreset,
  dashboardKeys,
  listDashboardPresets,
  listGadgetRenderers,
  previewDashboardPreset,
  type Dashboard,
  type DashboardPreset,
  type DashboardWarning,
  type PresetPreview,
} from './api';
import { DatasourcePicker, useSourceRows } from './gadget-editor';
import { templateName } from '@/core/renderer-labels';
import { sourcesLinkFor } from './sources-link';
import { BareButton } from '@/components/bare-button';

/** Where an applied preset lands: a new dashboard, appended below the current one's gadgets, or replacing them. */
type ApplyMode = 'create' | 'append' | 'replace';


/** Slot id to explicitly chosen datasource ids; slots without an entry stay unbound. */
type SlotSources = Record<string, string[]>;

/**
 * Drops empty selections so a request only names slots the owner actually bound.
 *
 * @param sources Draft selection per slot id.
 * @returns The `slot_sources` request body.
 */
function boundSlotSources(sources: SlotSources): SlotSources {
  return Object.fromEntries(Object.entries(sources).filter(([, ids]) => ids.length > 0));
}

/** True when a source's data shapes intersect the renderer's accepted shapes (null accepts any shape). */
function isAccepted(shapes: readonly DataShape[] | undefined, accepted: readonly DataShape[] | null): boolean {
  return accepted === null || (shapes ?? []).some((shape) => accepted.includes(shape));
}

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

/** Backend preview/apply 409 codes that have dedicated `dashboard.presetError.<code>` copy. */
const PRESET_ERROR_CODES = ['dashboard_limit', 'definition_limit', 'group_limit', 'instance_limit', 'preview_changed', 'revision_conflict', 'revision_exhausted'];

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
  const ts = useTranslations('uiSweep');
  const tg = useTranslations('gadgetSettings');
  const newNameId = useId();
  const confirmId = useId();
  const catalogHeadingId = useId();
  const layoutHeadingId = useId();
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();

  const [selectedPresetId, setSelectedPresetId] = useState<string | null>(null);
  const [applyMode, setApplyMode] = useState<ApplyMode>('create');
  const [newDashboardName, setNewDashboardName] = useState<string>('');
  const [replaceConfirmed, setReplaceConfirmed] = useState<boolean>(false);
  /** Localized copy for a failed preview/apply: the 409 code first (limits, stale preview), else the failure class. */
  const presetFailureText = (error: unknown): string => {
    if (error instanceof ApiError && error.code && PRESET_ERROR_CODES.includes(error.code)) return t(`presetError.${error.code}`);
    return tg(apiFailureKey(error) ?? 'requestFailed');
  };
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  // Explicit per-slot datasource choice; sent with preview AND apply so the preview fingerprint matches.
  const [slotSources, setSlotSources] = useState<SlotSources>({});
  // Rows load only while the dialog is open (the picker below shares this cache entry).
  const { query: sourceQuery, rows: sourceRows } = useSourceRows(open);
  /** Preset whose preselection waited for unloaded source pages; cleared once it has been re-run. */
  const latePreselectRef = useRef<string | null>(null);

  // Fetch standard presets catalog
  const presetsQuery = useQuery({
    queryKey: dashboardKeys.presets,
    queryFn: ({ signal }) => listDashboardPresets(signal),
    enabled: open,
  });

  // Renderer descriptors drive the shape-filtered "needs setup" link of each slot.
  const renderersQuery = useQuery({
    queryKey: dashboardKeys.renderers,
    queryFn: ({ signal }) => listGadgetRenderers(signal),
    enabled: open,
  });
  const renderersById = useMemo(
    () => new Map((renderersQuery.data ?? []).map((renderer) => [renderer.id, renderer])),
    [renderersQuery.data],
  );

  const rendererCatalogLoaded = renderersQuery.isSuccess;

  const presets = presetsQuery.data ?? [];
  const selectedPreset = presets.find((preset) => preset.id === selectedPresetId) ?? null;

  // Preview query mutation
  const previewMutation = useMutation({
    mutationFn: async ({ preset, sources, mode = applyMode }: { preset: DashboardPreset; sources: SlotSources; mode?: ApplyMode }) => {
      setPreviewError(null);
      return previewDashboardPreset(
        preset.id,
        {
          slot_sources: boundSlotSources(sources),
          // The fingerprint covers the target, so the preview must be requested for the mode that will be applied.
          target_dashboard_id: mode === 'create' ? null : currentDashboardId,
        },
        session.csrfToken,
      );
    },
    onError: (error) => {
      // Server messages are English-only: show localized copy keyed by the failure class instead.
      setPreviewError(presetFailureText(error));
    },
  });

  // `mutate` keeps a stable identity across renders (the mutation object does not), so effects can depend on it.
  const previewMutate = previewMutation.mutate;
  // `reset` is stable too; depending on the mutation objects would recreate handleClose on every render.
  const previewReset = previewMutation.reset;

  // Apply mutation
  const applyMutation = useMutation({
    mutationFn: async (params: { presetId: string; preview: PresetPreview }) => {
      setApplyError(null);
      return applyDashboardPreset(
        params.presetId,
        {
          preview_fingerprint: params.preview.preview_fingerprint,
          slot_sources: boundSlotSources(slotSources),
          mode: applyMode,
          name: applyMode === 'create' ? newDashboardName.trim() || params.preview.name : null,
          target_dashboard_id: applyMode === 'create' ? null : currentDashboardId,
          expected_revision: applyMode === 'create' ? null : currentDashboardRevision,
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
      setApplyError(presetFailureText(error));
    },
  });

  const applyReset = applyMutation.reset;

  /** Closes dialog and resets draft preview selection state. */
  const handleClose = useCallback(() => {
    setSelectedPresetId(null);
    setPreviewError(null);
    setApplyError(null);
    setReplaceConfirmed(false);
    setSlotSources({});
    latePreselectRef.current = null;
    previewReset();
    applyReset();
    onOpenChange(false);
  }, [applyReset, onOpenChange, previewReset]);

  const { hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage, isSuccess: sourcesLoaded } = sourceQuery;
  // Read every source page while the dialog is open (bounded by the page count) so "the only compatible source"
  // is decided on complete data; stops on a page error.
  useEffect(() => {
    if (open && hasNextPage && !isFetchingNextPage && !isFetchNextPageError) void fetchNextPage({ cancelRefetch: false });
  }, [open, hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage]);

  /**
   * Binds the only compatible active datasource of each source slot of a preset that is not yet in `existing`.
   * Slots with several (or no) compatible sources stay unbound so the owner chooses deliberately; nothing is
   * bound while source pages, the first page or the renderer catalog are still loading, because a lone compatible row may not be the only one.
   */
  const preselect = useCallback(
    (preset: DashboardPreset, existing: SlotSources): SlotSources => {
      const initial: SlotSources = {};
      if (hasNextPage || !sourcesLoaded || !rendererCatalogLoaded) return initial;
      for (const slot of preset.slots) {
        if (slot.slot_id in existing) continue;
        const descriptor = renderersById.get(slot.renderer);
        // Preset maps are live (provider-seeded), so there is nothing to preselect.
        if (!descriptor || descriptor.source_mode === 'none' || slot.renderer === 'map') continue;
        const compatible = sourceRows.filter((row) => row.status === 'active' && isAccepted(row.data_shapes, descriptor.accepted_shapes));
        if (compatible.length === 1) initial[slot.slot_id] = [compatible[0].id];
      }
      return initial;
    },
    [hasNextPage, rendererCatalogLoaded, renderersById, sourceRows, sourcesLoaded],
  );

  // Re-runs the preselection for still-unset slots once the pages that were missing at click time have loaded.
  // A slot the owner already touched has a key in `slotSources` and is never overwritten.
  useEffect(() => {
    if (!selectedPreset || latePreselectRef.current !== selectedPreset.id || hasNextPage || !sourcesLoaded || !rendererCatalogLoaded) return;
    latePreselectRef.current = null;
    const extra = preselect(selectedPreset, slotSources);
    if (Object.keys(extra).length === 0) return;
    const next = { ...slotSources, ...extra };
    // One-shot (guarded by the ref): the late preselection is a reaction to the source query finishing, not derivable during render.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- guarded one-shot sync to the late-arriving source query result.
    setSlotSources(next);
    previewMutate({ preset: selectedPreset, sources: next });
  }, [hasNextPage, preselect, previewMutate, rendererCatalogLoaded, selectedPreset, slotSources, sourcesLoaded]);

  /**
   * Selects a preset, preselects the only compatible active datasource of each source slot, and previews it.
   * When source pages are still loading the preselection is re-run once they have all arrived.
   */
  const handleSelectPreset = useCallback(
    (preset: DashboardPreset) => {
      const initial = preselect(preset, {});
      latePreselectRef.current = hasNextPage || !sourcesLoaded || !rendererCatalogLoaded ? preset.id : null;
      setSelectedPresetId(preset.id);
      setNewDashboardName(preset.label);
      setPreviewError(null);
      setApplyError(null);
      setSlotSources(initial);
      void previewMutation.mutateAsync({ preset, sources: initial });
    },
    [hasNextPage, preselect, previewMutation, rendererCatalogLoaded, sourcesLoaded],
  );

  /** Records a slot's datasource choice and re-previews so readiness and the fingerprint reflect it. */
  const handleSlotSources = useCallback(
    (slotId: string, ids: string[]) => {
      if (!selectedPreset) return;
      const next = { ...slotSources, [slotId]: ids };
      setSlotSources(next);
      previewMutation.mutate({ preset: selectedPreset, sources: next });
    },
    [previewMutation, selectedPreset, slotSources],
  );

  /**
   * Switches create/append/replace and re-previews, because the preview fingerprint includes the target and
   * applying with a create-mode fingerprint would always conflict (`preview_changed`).
   */
  const handleApplyMode = useCallback(
    (mode: ApplyMode) => {
      setApplyMode(mode);
      if (selectedPreset && mode !== applyMode) previewMutation.mutate({ preset: selectedPreset, sources: slotSources, mode });
    },
    [applyMode, previewMutation, selectedPreset, slotSources],
  );

  /** Confirms and applies the active preview. */
  const handleApply = useCallback(() => {
    if (!selectedPreset || !previewMutation.data) return;
    if (applyMode === 'replace' && !replaceConfirmed) return;
    // mutate (not mutateAsync): failures are reported by onError, never as an unhandled rejection.
    applyMutation.mutate({
      presetId: selectedPreset.id,
      preview: previewMutation.data,
    });
  }, [applyMode, applyMutation, previewMutation.data, replaceConfirmed, selectedPreset]);

  // Aggregate missing access / capability warnings across all slots
  const allWarnings: DashboardWarning[] =
    previewMutation.data?.slots.flatMap((slot) => slot.warnings) ?? [];
  /** Display name of a slot: the preset's own label ("Live news"), else the renderer's template name. */
  const slotName = (slotId: string, renderer: string): string =>
    selectedPreset?.slots.find((slot) => slot.slot_id === slotId)?.label ?? templateName(renderer, tg);
  const modes: { mode: ApplyMode; label: string; needsTarget: boolean }[] = [
    { mode: 'create', label: t('applyModeCreate'), needsTarget: false },
    { mode: 'append', label: t('applyModeAppend'), needsTarget: true },
    { mode: 'replace', label: t('applyModeReplace'), needsTarget: true },
  ];

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="flex h-[min(88dvh,52rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-6xl">
        <DialogHeader className="border-b border-border px-6 py-4">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
            <LayoutTemplate className="size-4 text-muted-foreground" aria-hidden />
            {t('presetPickerTitle')}
          </DialogTitle>
          <DialogDescription className="text-sm text-muted-foreground">{t('presetPickerDesc')}</DialogDescription>
        </DialogHeader>

        {/* Three panes on desktop (catalog, layout, target and sources); one scrolling column on phones. */}
        <div className="grid min-h-0 flex-1 grid-cols-1 overflow-y-auto md:grid-cols-[15rem_minmax(0,1fr)_22rem] md:overflow-hidden">
          <section aria-labelledby={catalogHeadingId} className="flex min-h-0 flex-col border-border md:border-r">
            <h3 id={catalogHeadingId} className="flex items-center justify-between px-4 pt-4 pb-2 text-sm font-semibold text-foreground">
              <span>{t('selectPreset')}</span>
              {presetsQuery.isLoading && <span className="text-xs font-normal text-muted-foreground motion-safe:animate-pulse">{ts('ppLoading')}</span>}
            </h3>
            <div className="space-y-1 px-2 pb-4 md:min-h-0 md:flex-1 md:overflow-y-auto">
              {presets.map((preset) => {
                const isSelected = preset.id === selectedPresetId;
                return (
                  <BareButton
                    key={preset.id}
                    type="button"
                    aria-pressed={isSelected}
                    onClick={() => handleSelectPreset(preset)}
                    className={`flex w-full items-start justify-between gap-2 rounded-md px-3 py-2.5 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                      isSelected ? 'bg-primary/10 text-foreground' : 'hover:bg-muted'
                    }`}
                  >
                    <span className="min-w-0">
                      <span className="block text-sm font-semibold text-foreground">{preset.label}</span>
                      <span className="mt-0.5 line-clamp-2 block text-xs text-muted-foreground">
                        {t('presetSlotsCount', { count: preset.slots.length })}: {preset.slots.map((s) => s.label ?? templateName(s.renderer, tg)).join(', ')}
                      </span>
                    </span>
                    <ChevronRight className={`mt-0.5 size-4 shrink-0 ${isSelected ? 'text-primary' : 'text-muted-foreground'}`} aria-hidden />
                  </BareButton>
                );
              })}
            </div>
          </section>

          <section aria-labelledby={layoutHeadingId} className="flex min-h-0 flex-col gap-3 bg-muted/40 p-4 md:overflow-y-auto">
            <div className="flex items-baseline justify-between gap-3">
              <h3 id={layoutHeadingId} className="text-sm font-semibold text-foreground">
                {selectedPreset ? selectedPreset.label : t('layoutPreview')}
                {selectedPreset && (
                  <span className="ml-2 text-xs font-normal text-muted-foreground">{t('presetGadgetsCount', { count: selectedPreset.slots.length })}</span>
                )}
              </h3>
              {previewMutation.data && (
                <span className="text-xs text-muted-foreground">
                  {t('slotsReadiness', {
                    ready: previewMutation.data.slots.filter((slot) => slot.warnings.length === 0).length,
                    setup: previewMutation.data.slots.filter((slot) => slot.warnings.length > 0).length,
                  })}
                </span>
              )}
            </div>
            {previewMutation.isPending && <p className="text-xs text-muted-foreground motion-safe:animate-pulse">{t('previewingPreset')}</p>}
            {previewError && (
              <div role="alert" className="flex items-center gap-2 rounded-md bg-destructive/10 p-2.5 text-xs text-destructive">
                <AlertCircle className="size-4 shrink-0" aria-hidden />
                <span>{previewError}</span>
              </div>
            )}
            {!selectedPreset ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 py-12 text-center text-sm text-muted-foreground">
                <Sparkles className="size-6 opacity-60" aria-hidden />
                <p>{t('layoutPreviewEmpty')}</p>
              </div>
            ) : previewMutation.data?.layouts ? (
              <PresetLayoutPreview
                append={applyMode === 'append'}
                layout={previewMutation.data.layouts.desktop}
                slots={previewMutation.data.slots.map((slot) => ({ id: slot.slot_id, name: slotName(slot.slot_id, slot.renderer), ready: slot.warnings.length === 0 }))}
              />
            ) : null}
            {allWarnings.length > 0 && (
              <div className="space-y-1.5 rounded-md bg-background p-3 text-xs text-foreground">
                <div className="flex items-center gap-1.5 font-semibold">
                  <AlertCircle className="size-4 shrink-0" aria-hidden />
                  <span>{t('missingAccessWarnings')} ({allWarnings.length})</span>
                </div>
                <ul className="list-inside list-disc space-y-0.5 pl-1">
                  {allWarnings.slice(0, 5).map((warning, idx) => (
                    <li key={idx}>
                      {warning.capability
                        ? ts('ppCapability', { capability: warning.capability })
                        : t.has(`presetWarning.${warning.code}`) ? t(`presetWarning.${warning.code}` as 'cancel') : warning.code}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>

          <section className="flex min-h-0 flex-col gap-4 border-border p-4 md:overflow-y-auto md:border-l">
            {selectedPreset && previewMutation.data && (
              <>
                <fieldset className="space-y-2">
                  <legend className="mb-2 text-sm font-semibold text-foreground">{t('applyMode')}</legend>
                  <div className="grid grid-cols-1 gap-1 rounded-md bg-muted p-1">
                    {modes.map(({ mode, label, needsTarget }) => (
                      <BareButton
                        key={mode}
                        type="button"
                        aria-pressed={applyMode === mode}
                        disabled={needsTarget && !currentDashboardId}
                        onClick={() => handleApplyMode(mode)}
                        className={`rounded px-3 py-2 text-left text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40 ${
                          applyMode === mode
                            ? mode === 'replace' ? 'bg-background text-destructive' : 'bg-background text-foreground'
                            : 'text-muted-foreground hover:text-foreground'
                        }`}
                      >
                        {label}
                      </BareButton>
                    ))}
                  </div>
                  {applyMode === 'create' && (
                    <div className="space-y-1 pt-1">
                      <Label htmlFor={newNameId} className="text-xs">{t('dashboardName')}</Label>
                      <Input id={newNameId} value={newDashboardName} onChange={(e) => setNewDashboardName(e.target.value)} placeholder={selectedPreset.label} className="h-8 text-xs" />
                    </div>
                  )}
                  {applyMode === 'append' && <p className="text-xs text-muted-foreground">{t('appendHint')}</p>}
                  {applyMode === 'replace' && (
                    <div className="space-y-2 rounded-md bg-destructive/10 p-2.5 text-xs">
                      <p className="font-medium leading-relaxed text-destructive">{t('confirmReplaceWarning')}</p>
                      <div className="flex items-center gap-2">
                        <Checkbox id={confirmId} checked={replaceConfirmed} onCheckedChange={(checked) => setReplaceConfirmed(Boolean(checked))} />
                        <label htmlFor={confirmId} className="cursor-pointer text-xs font-medium leading-none text-foreground">{t('confirmReplaceCheckbox')}</label>
                      </div>
                    </div>
                  )}
                </fieldset>

                {/* Per-gadget readiness: warnings come from the preview; nothing is connected on the owner's behalf */}
                <div className="space-y-1.5">
                  <ul className="divide-y divide-border rounded-md border border-border">
                    {previewMutation.data.slots.map((slot) => {
                      const descriptor = renderersById.get(slot.renderer);
                      // Live map slots take no datasource: the backend seeds providers, so no picker is shown.
                      const isLiveMap = slot.filters?.map_mode === 'live';
                      const bindsSources = !isLiveMap && (descriptor?.source_mode === 'single' || descriptor?.source_mode === 'multi');
                      // An unbound slot is fixed by the selector below and a missing scope is set in the gadget editor after applying;
                      // only the remaining warnings need the Sources page.
                      const needsSourcesPage = slot.warnings.some((warning) => warning.code !== 'source_unbound' && warning.code !== 'scope_required');
                      return (
                        <li key={slot.slot_id} className="space-y-2 px-2.5 py-2 text-xs">
                          <div className="flex items-center justify-between gap-2">
                            <span className="min-w-0 truncate font-medium">{slotName(slot.slot_id, slot.renderer)}</span>
                            {slot.warnings.length === 0 ? (
                              <span className="shrink-0 text-muted-foreground">{t('slotReady')}</span>
                            ) : (
                              <span className="flex shrink-0 items-center gap-2">
                                <span className="font-semibold text-destructive">{t('slotNeedsSetup')}</span>
                                {/* Renderers that bind no sources have nothing to configure in Sources. */}
                                {descriptor?.source_mode !== 'none' && needsSourcesPage && <Link href={descriptor ? sourcesLinkFor(descriptor) : '/settings/sources'} className="inline-flex min-h-9 pointer-coarse:min-h-11 items-center text-primary underline underline-offset-4">{t('slotConfigure')}</Link>}
                              </span>
                            )}
                          </div>
                          {bindsSources && (
                            <DatasourcePicker
                              acceptedShapes={descriptor.accepted_shapes}
                              mode={descriptor.source_mode === 'single' ? 'single' : 'multi'}
                              value={slotSources[slot.slot_id] ?? []}
                              onChange={(ids) => handleSlotSources(slot.slot_id, ids)}
                              sourcesHref={sourcesLinkFor(descriptor)}
                              max={descriptor.max_sources ?? undefined}
                            />
                          )}
                        </li>
                      );
                    })}
                  </ul>
                  <p className="text-xs text-muted-foreground">{t('presetsNoConnect')}</p>
                </div>
              </>
            )}
          </section>
        </div>

        {applyError && (
          <div role="alert" className="flex items-center justify-between border-t border-destructive/20 bg-destructive/10 px-6 py-2.5 text-xs text-destructive">
            <div className="flex items-center gap-2">
              <AlertCircle className="size-4 shrink-0" aria-hidden />
              <span>{applyError}</span>
            </div>
            <BareButton type="button" aria-label={t('cancel')} onClick={() => setApplyError(null)} className="rounded p-1 hover:bg-destructive/10">
              <X className="size-3.5" aria-hidden />
            </BareButton>
          </div>
        )}

        <DialogFooter className="flex items-center justify-between gap-2 border-t border-border px-6 py-3 sm:justify-between">
          <Button type="button" variant="outline" onClick={handleClose}>
            {t('cancel')}
          </Button>
          <Button
            type="button"
            variant={applyMode === 'replace' ? 'destructive' : 'default'}
            disabled={
              !selectedPreset ||
              !previewMutation.data ||
              previewMutation.isPending ||
              applyMutation.isPending ||
              (applyMode === 'replace' && !replaceConfirmed)
            }
            onClick={handleApply}
          >
            {applyMutation.isPending ? (
              t('applyingPreset')
            ) : (
              <span className="flex items-center gap-1.5">
                <CheckCircle2 className="size-4" aria-hidden />
                <span>{t('applyPreset')}</span>
              </span>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** One proposed gadget in the layout miniature. */
type LayoutSlot = { id: string; name: string; ready: boolean };

/**
 * Draws the preset's proposed desktop rectangles on the dashboard's own column grid, so the owner sees the
 * arrangement (map beside live news, tiles below) before applying. It duplicates the slot list visually,
 * so it is hidden from assistive technology; in append mode a dashed band marks the existing gadgets kept above.
 *
 * @param layout Proposed desktop breakpoint from the preview (columns and slot rectangles).
 * @param slots Display name and readiness per slot id.
 * @param append Whether the preset is added below existing gadgets.
 */
function PresetLayoutPreview({ layout, slots, append }: {
  layout: PresetPreview['layouts']['desktop'];
  slots: LayoutSlot[];
  append: boolean;
}) {
  const byId = new Map(slots.map((slot) => [slot.id, slot]));
  return (
    <div aria-hidden className="space-y-1">
      {append && <div className="h-6 rounded-sm border border-dashed border-border bg-background/60" />}
      <div className="grid gap-1" style={{ gridTemplateColumns: `repeat(${layout.columns}, minmax(0, 1fr))`, gridAutoRows: '0.875rem' }}>
        {layout.items.map((item) => {
          const slot = byId.get(item.slot_id);
          return (
            <div
              key={item.slot_id}
              style={{ gridColumn: `${item.x + 1} / span ${item.w}`, gridRow: `${item.y + 1} / span ${item.h}` }}
              className={`flex min-w-0 flex-col justify-between rounded-sm bg-background p-1.5 ${slot?.ready ? 'border border-border' : 'border border-dashed border-destructive/60'}`}
            >
              <span className="truncate text-[11px] font-medium text-foreground">{slot?.name ?? item.slot_id}</span>
              {!slot?.ready && <span className="size-1.5 self-end rounded-full bg-destructive" />}
            </div>
          );
        })}
      </div>
    </div>
  );
}
