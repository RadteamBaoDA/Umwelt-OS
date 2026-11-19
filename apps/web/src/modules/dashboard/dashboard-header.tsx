'use client';

import { AlignJustify, ChevronDown, Edit3, Eye, Pencil, Plus, Sparkles, Trash2 } from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useEffect, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { applyDensity, readDensity, type DensityId } from '@/core/density';
import { BareButton } from '@/components/bare-button';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import type { Dashboard } from './api';

/** Formats an epoch-ms timestamp as a localized relative age ("5 minutes ago"). */
export function formatRelativeAge(ms: number, locale: string): string {
  const diffMin = Math.round((ms - Date.now()) / 60_000);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
  if (Math.abs(diffMin) < 60) return rtf.format(diffMin, 'minute');
  if (Math.abs(diffMin) < 1440) return rtf.format(Math.round(diffMin / 60), 'hour');
  return rtf.format(Math.round(diffMin / 1440), 'day');
}

/** Props of the dashboard switcher menu. */
interface SwitcherProps {
  dashboards: { id: string; name: string; revision: number }[];
  activeDashboard: Dashboard | null;
  selectedDashboardId: string | null;
  layoutOperationPending: boolean;
  isAdminViewer: boolean;
  onSwitch: (id: string) => void;
  onCreate: () => void;
  onRename: () => void;
  onDelete: () => void;
  onOpenPresets: () => void;
}

/**
 * Title-as-menu dashboard switcher. Owner-only entries (create, rename, delete, presets) are hidden for
 * read-only administrators; every entry is disabled while a layout operation is pending.
 */
export function DashboardSwitcher(props: SwitcherProps) {
  const t = useTranslations('dashboard');
  const { dashboards, activeDashboard, selectedDashboardId, layoutOperationPending, isAdminViewer } = props;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <BareButton
          type="button"
          disabled={layoutOperationPending}
          aria-label={t('switchDashboard')}
          className="inline-flex min-h-9 pointer-coarse:min-h-11 items-center gap-2 rounded-lg border border-transparent bg-transparent py-0 pl-0 pr-2 text-left text-2xl font-semibold leading-tight tracking-[-0.02em] text-foreground hover:bg-primary/10"
        >
          <span className="truncate">{activeDashboard?.name ?? t('title')}</span>
          <ChevronDown className="size-5 text-muted-foreground" aria-hidden="true" />
        </BareButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56">
        {dashboards.map((dash) => (
          <DropdownMenuItem
            key={dash.id}
            disabled={layoutOperationPending}
            onClick={() => props.onSwitch(dash.id)}
            className={`text-xs flex items-center justify-between ${
              dash.id === selectedDashboardId ? 'font-semibold text-foreground' : ''
            }`}
          >
            <span className="truncate">{dash.name}</span>
            <span className="text-xs text-muted-foreground tabular-nums">r.{dash.revision}</span>
          </DropdownMenuItem>
        ))}

        <DropdownMenuSeparator />

        {!isAdminViewer && (
          <DropdownMenuItem disabled={layoutOperationPending} onClick={props.onCreate} className="text-xs gap-2 font-medium">
            <Plus className="w-3.5 h-3.5" />
            <span>{t('newDashboard')}</span>
          </DropdownMenuItem>
        )}

        {!isAdminViewer && activeDashboard && (
          <>
            <DropdownMenuItem disabled={layoutOperationPending} onClick={props.onRename} className="text-xs gap-2">
              <Pencil className="w-3.5 h-3.5" />
              <span>{t('renameDashboard.label')}</span>
            </DropdownMenuItem>

            <DropdownMenuItem
              disabled={layoutOperationPending}
              onClick={props.onDelete}
              className="text-xs gap-2 text-destructive focus:text-destructive"
            >
              <Trash2 className="w-3.5 h-3.5" />
              <span>{t('deleteDashboard.label')}</span>
            </DropdownMenuItem>
          </>
        )}

        {!isAdminViewer && <DropdownMenuSeparator />}

        {!isAdminViewer && (
          <DropdownMenuItem disabled={layoutOperationPending} onClick={props.onOpenPresets} className="text-xs gap-2">
            <Sparkles className="w-3.5 h-3.5 text-muted-foreground" />
            <span>{t('presets.label')}</span>
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Comfortable/Compact density toggle. The pre-paint script already set `data-density`; state is read from <html>
 * lazily (this header only renders after the session loads, client-side) and changes are applied and persisted through `applyDensity`.
 */
export function DensityToggle() {
  const t = useTranslations('dashboard');
  const [density, setDensity] = useState<DensityId>(() => (typeof document === 'undefined' ? 'comfortable' : readDensity()));
  const compact = density === 'compact';
  return (
    <Button
      type="button"
      variant={compact ? 'default' : 'outline'}
      aria-pressed={compact}
      aria-label={`${t('density.label')}: ${compact ? t('density.compact') : t('density.comfortable')}`}
      title={t('density.label')}
      onClick={() => {
        const next = compact ? 'comfortable' : 'compact';
        applyDensity(next);
        setDensity(next);
      }}
    >
      <AlignJustify className="w-4 h-4" aria-hidden="true" />
      <span>{compact ? t('density.compact') : t('density.comfortable')}</span>
    </Button>
  );
}

/** Props of the header summary and action row. */
interface HeaderProps extends SwitcherProps {
  /** Pre-joined "updated · stale" summary, or null when there is nothing to say. */
  summary: string | null;
  /** Overview greeting strip rendered under the title (omitted on other tabs). */
  greeting?: ReactNode;
  /** Optional workspace content-translation popover rendered next to the density toggle. */
  translationControl?: ReactNode;
  isOwner: boolean;
  isEditMode: boolean;
  canEditLayout: boolean;
  onToggleEdit: () => void;
  /** Terminal-skin header shortcut: enters edit mode and opens the Add gadget dialog. */
  onAddGadget: () => void;
  onOpenManagement: () => void;
}

/** Returns the app shell's statusline slot element, or null when no shell footer is mounted. */
const findStatuslineSlot = (): HTMLElement | null => document.getElementById('statusline-info');

/**
 * Renders the freshness summary into the app shell's statusline footer (`#statusline-info`) when that slot
 * exists, and inline under the title otherwise (the slot is absent on routes without the shell footer).
 *
 * The slot is looked up once, after mount, in an effect. Until then (SSR and the first client pass) nothing
 * renders, so the summary never flashes inline before it moves into the footer. The lookup is not repeated, so a
 * shell that mounts after this header is not picked up until the header remounts. Side effects: reads the DOM only.
 * Renders nothing for a null summary.
 *
 * @param props.summary Pre-joined summary text, or null to render nothing.
 */
export function StatuslineSummary({ summary }: { summary: string | null }) {
  // undefined = not looked up yet (SSR / first pass); null = no shell footer mounted; Element = portal target.
  const [slot, setSlot] = useState<Element | null | undefined>(undefined);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time post-mount read of the DOM slot; it cannot be known during render
    setSlot(findStatuslineSlot());
  }, []);
  if (!summary || slot === undefined) return null;
  if (slot) return createPortal(<span>{summary}</span>, slot);
  return <p className="m-0 text-sm text-muted-foreground">{summary}</p>;
}

/**
 * Page header (Dashboard artboard): switcher-as-title, optional greeting under the title, freshness summary
 * (portalled into the statusline when available) and the edit / manage actions.
 */
export function DashboardHeader(props: HeaderProps) {
  const t = useTranslations('dashboard');
  const { summary, greeting, isOwner, isAdminViewer, isEditMode, canEditLayout, layoutOperationPending } = props;
  return (
    <div className="dash-header flex flex-wrap items-end gap-3">
      <div className="mr-auto flex min-w-0 basis-full flex-col gap-0.5 sm:basis-auto">
        <h1 className="m-0 min-w-0 text-2xl font-semibold leading-tight tracking-[-0.02em] text-foreground">
          <DashboardSwitcher {...props} />
        </h1>
        {greeting && <div className="dash-greeting">{greeting}</div>}
        <StatuslineSummary summary={summary} />
      </div>
      <DensityToggle />
      {props.translationControl}
      {!isAdminViewer && (
        <Button
          type="button"
          variant={isEditMode ? 'default' : 'outline'}
          className="font-semibold"
          disabled={layoutOperationPending || (!canEditLayout && !isEditMode)}
          onClick={props.onToggleEdit}
        >
          {isEditMode ? <Eye className="w-4 h-4" aria-hidden="true" /> : <Edit3 className="w-4 h-4" aria-hidden="true" />}
          <span>{isEditMode ? t('finishEditing') : t('toggleEdit')}</span>
        </Button>
      )}
      {isOwner && (
        <Button type="button" variant="outline" onClick={props.onOpenManagement}>
          {t('manage.topicsAndHighlights')}
        </Button>
      )}
      {/* Terminal-only shortcut (hidden by CSS in the default skin); edit mode already has its own Add gadget button. */}
      {!isAdminViewer && canEditLayout && !isEditMode && (
        <Button type="button" variant="outline" className="dash-add-gadget" disabled={layoutOperationPending} onClick={props.onAddGadget}>
          <Plus className="w-4 h-4" aria-hidden="true" />
          <span>{t('addGadget.label')}</span>
        </Button>
      )}
    </div>
  );
}
