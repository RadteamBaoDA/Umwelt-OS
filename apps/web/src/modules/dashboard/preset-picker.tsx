'use client';

import { AlertCircle, CheckCircle2, ChevronRight, LayoutTemplate, Sparkles, X } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { useId } from 'react';
import { BareButton } from '@/components/bare-button';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { templateName } from '@/core/renderer-labels';
import type { Dashboard, DashboardWarning, PresetPreview } from './api';
import { DatasourcePicker } from './gadget-editor';
import { sourcesLinkFor } from './sources-link';
import { usePresetPicker, type ApplyMode, type PresetPickerModel } from './use-preset-picker';

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

/** Display name of a slot: the preset's own label ("Live news"), else the renderer's template name. */
function useSlotName(model: PresetPickerModel) {
  const tg = useTranslations('gadgetSettings');
  return (slotId: string, renderer: string): string =>
    model.selectedPreset?.slots.find((slot) => slot.slot_id === slotId)?.label ?? templateName(renderer, tg);
}

/** Left pane: the presets catalog. */
function CatalogPane({ model, headingId }: { model: PresetPickerModel; headingId: string }) {
  const t = useTranslations('dashboard');
  const ts = useTranslations('uiSweep');
  const tg = useTranslations('gadgetSettings');
  const { presets, draft, catalog, actions } = model;
  return (
    <section aria-labelledby={headingId} className="flex min-h-0 flex-col border-border md:border-r">
      <h3 id={headingId} className="flex items-center justify-between px-4 pt-4 pb-2 text-sm font-semibold text-foreground">
        <span>{t('selectPreset')}</span>
        {catalog.presetsQuery.isLoading && <span className="text-xs font-normal text-muted-foreground motion-safe:animate-pulse">{ts('ppLoading')}</span>}
      </h3>
      <div className="space-y-1 px-2 pb-4 md:min-h-0 md:flex-1 md:overflow-y-auto">
        {presets.map((preset) => {
          const isSelected = preset.id === draft.selectedPresetId;
          return (
            <BareButton
              key={preset.id}
              type="button"
              aria-pressed={isSelected}
              onClick={() => actions.handleSelectPreset(preset)}
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
  );
}

/** Aggregated missing access / capability warnings across all slots (first five). */
function WarningsBox({ warnings }: { warnings: DashboardWarning[] }) {
  const t = useTranslations('dashboard');
  const ts = useTranslations('uiSweep');
  if (warnings.length === 0) return null;
  return (
    <div className="space-y-1.5 rounded-md bg-background p-3 text-xs text-foreground">
      <div className="flex items-center gap-1.5 font-semibold">
        <AlertCircle className="size-4 shrink-0" aria-hidden />
        <span>{t('missingAccessWarnings')} ({warnings.length})</span>
      </div>
      <ul className="list-inside list-disc space-y-0.5 pl-1">
        {warnings.slice(0, 5).map((warning, idx) => (
          <li key={idx}>
            {warning.capability
              ? ts('ppCapability', { capability: warning.capability })
              : t.has(`presetWarning.${warning.code}`) ? t(`presetWarning.${warning.code}` as 'cancel') : warning.code}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Middle pane: layout miniature, readiness counts, preview errors and warnings. */
function LayoutPane({ model, headingId }: { model: PresetPickerModel; headingId: string }) {
  const t = useTranslations('dashboard');
  const slotName = useSlotName(model);
  const { selectedPreset, mutations, draft } = model;
  const { previewMutation } = mutations;
  const preview = previewMutation.data;
  const allWarnings: DashboardWarning[] = preview?.slots.flatMap((slot) => slot.warnings) ?? [];
  return (
    <section aria-labelledby={headingId} className="flex min-h-0 flex-col gap-3 bg-muted/40 p-4 md:overflow-y-auto">
      <div className="flex items-baseline justify-between gap-3">
        <h3 id={headingId} className="text-sm font-semibold text-foreground">
          {selectedPreset ? selectedPreset.label : t('layoutPreview')}
          {selectedPreset && (
            <span className="ml-2 text-xs font-normal text-muted-foreground">{t('presetGadgetsCount', { count: selectedPreset.slots.length })}</span>
          )}
        </h3>
        {preview && (
          <span className="text-xs text-muted-foreground">
            {t('slotsReadiness', {
              ready: preview.slots.filter((slot) => slot.warnings.length === 0).length,
              setup: preview.slots.filter((slot) => slot.warnings.length > 0).length,
            })}
          </span>
        )}
      </div>
      {previewMutation.isPending && <p className="text-xs text-muted-foreground motion-safe:animate-pulse">{t('previewingPreset')}</p>}
      {draft.previewError && (
        <div role="alert" className="flex items-center gap-2 rounded-md bg-destructive/10 p-2.5 text-xs text-destructive">
          <AlertCircle className="size-4 shrink-0" aria-hidden />
          <span>{draft.previewError}</span>
        </div>
      )}
      {!selectedPreset ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 py-12 text-center text-sm text-muted-foreground">
          <Sparkles className="size-6 opacity-60" aria-hidden />
          <p>{t('layoutPreviewEmpty')}</p>
        </div>
      ) : preview?.layouts ? (
        <PresetLayoutPreview
          append={draft.applyMode === 'append'}
          layout={preview.layouts.desktop}
          slots={preview.slots.map((slot) => ({ id: slot.slot_id, name: slotName(slot.slot_id, slot.renderer), ready: slot.warnings.length === 0 }))}
        />
      ) : null}
      <WarningsBox warnings={allWarnings} />
    </section>
  );
}

/** Apply-mode switch plus the mode-specific inputs (new name, append hint, replace confirmation). */
function ApplyModeFieldset({ model, target }: { model: PresetPickerModel; target: boolean }) {
  const t = useTranslations('dashboard');
  const newNameId = useId();
  const confirmId = useId();
  const { draft, actions, selectedPreset } = model;
  const { applyMode } = draft;
  const modes: { mode: ApplyMode; label: string; needsTarget: boolean }[] = [
    { mode: 'create', label: t('applyModeCreate'), needsTarget: false },
    { mode: 'append', label: t('applyModeAppend'), needsTarget: true },
    { mode: 'replace', label: t('applyModeReplace'), needsTarget: true },
  ];
  return (
    <fieldset className="space-y-2">
      <legend className="mb-2 text-sm font-semibold text-foreground">{t('applyMode')}</legend>
      <div className="grid grid-cols-1 gap-1 rounded-md bg-muted p-1">
        {modes.map(({ mode, label, needsTarget }) => (
          <BareButton
            key={mode}
            type="button"
            aria-pressed={applyMode === mode}
            disabled={needsTarget && !target}
            onClick={() => actions.handleApplyMode(mode)}
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
          <Input id={newNameId} value={draft.newDashboardName} onChange={(e) => draft.setNewDashboardName(e.target.value)} placeholder={selectedPreset?.label} className="h-8 text-xs" />
        </div>
      )}
      {applyMode === 'append' && <p className="text-xs text-muted-foreground">{t('appendHint')}</p>}
      {applyMode === 'replace' && (
        <div className="space-y-2 rounded-md bg-destructive/10 p-2.5 text-xs">
          <p className="font-medium leading-relaxed text-destructive">{t('confirmReplaceWarning')}</p>
          <div className="flex items-center gap-2">
            <Checkbox id={confirmId} checked={draft.replaceConfirmed} onCheckedChange={(checked) => draft.setReplaceConfirmed(Boolean(checked))} />
            <label htmlFor={confirmId} className="cursor-pointer text-xs font-medium leading-none text-foreground">{t('confirmReplaceCheckbox')}</label>
          </div>
        </div>
      )}
    </fieldset>
  );
}

/** One slot's readiness row with its optional datasource picker. */
function SlotRow({ model, slot }: { model: PresetPickerModel; slot: PresetPreview['slots'][number] }) {
  const t = useTranslations('dashboard');
  const slotName = useSlotName(model);
  const descriptor = model.catalog.renderersById.get(slot.renderer);
  // Live map slots take no datasource: the backend seeds providers, so no picker is shown.
  const isLiveMap = slot.filters?.map_mode === 'live';
  const bindsSources = !isLiveMap && (descriptor?.source_mode === 'single' || descriptor?.source_mode === 'multi');
  // An unbound slot is fixed by the selector below and a missing scope is set in the gadget editor after applying;
  // only the remaining warnings need the Sources page.
  const needsSourcesPage = slot.warnings.some((warning) => warning.code !== 'source_unbound' && warning.code !== 'scope_required');
  return (
    <li className="space-y-2 px-2.5 py-2 text-xs">
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
          value={model.draft.slotSources[slot.slot_id] ?? []}
          onChange={(ids) => model.actions.handleSlotSources(slot.slot_id, ids)}
          sourcesHref={sourcesLinkFor(descriptor)}
          max={descriptor.max_sources ?? undefined}
        />
      )}
    </li>
  );
}

/** Right pane: apply mode and per-gadget readiness with datasource selection. */
function TargetPane({ model, hasTarget }: { model: PresetPickerModel; hasTarget: boolean }) {
  const t = useTranslations('dashboard');
  const preview = model.mutations.previewMutation.data;
  return (
    <section className="flex min-h-0 flex-col gap-4 border-border p-4 md:overflow-y-auto md:border-l">
      {model.selectedPreset && preview && (
        <>
          <ApplyModeFieldset model={model} target={hasTarget} />

          {/* Per-gadget readiness: warnings come from the preview; nothing is connected on the owner's behalf */}
          <div className="space-y-1.5">
            <ul className="divide-y divide-border rounded-md border border-border">
              {preview.slots.map((slot) => <SlotRow key={slot.slot_id} model={model} slot={slot} />)}
            </ul>
            <p className="text-xs text-muted-foreground">{t('presetsNoConnect')}</p>
          </div>
        </>
      )}
    </section>
  );
}

/** Footer: Cancel and the Apply button (disabled until a preview exists and, for replace, the confirmation is ticked). */
function PickerFooter({ model }: { model: PresetPickerModel }) {
  const t = useTranslations('dashboard');
  const { draft, mutations, actions, selectedPreset } = model;
  const { previewMutation, applyMutation } = mutations;
  const applyDisabled = !selectedPreset || !previewMutation.data || previewMutation.isPending
    || applyMutation.isPending || (draft.applyMode === 'replace' && !draft.replaceConfirmed);
  return (
    <DialogFooter className="flex items-center justify-between gap-2 border-t border-border px-6 py-3 sm:justify-between">
      <Button type="button" variant="outline" onClick={mutations.handleClose}>
        {t('cancel')}
      </Button>
      <Button
        type="button"
        variant={draft.applyMode === 'replace' ? 'destructive' : 'default'}
        disabled={applyDisabled}
        onClick={actions.handleApply}
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
  );
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
  const catalogHeadingId = useId();
  const layoutHeadingId = useId();
  const model = usePresetPicker(open, onOpenChange, { currentDashboardId, currentDashboardRevision }, onPresetApplied);
  const { applyError, setApplyError } = model.draft;

  return (
    <Dialog open={open} onOpenChange={model.mutations.handleClose}>
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
          <CatalogPane model={model} headingId={catalogHeadingId} />
          <LayoutPane model={model} headingId={layoutHeadingId} />
          <TargetPane model={model} hasTarget={Boolean(currentDashboardId)} />
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

        <PickerFooter model={model} />
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
