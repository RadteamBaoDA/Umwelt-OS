'use client';

import { ChevronDown, Edit3, Eye, Pencil, Plus, Sparkles, Trash2 } from 'lucide-react';
import { useTranslations } from 'next-intl';
import React from 'react';
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

        {!isAdminViewer && <DropdownMenuItem disabled={layoutOperationPending} onClick={props.onCreate} className="text-xs gap-2 font-medium">
          <Plus className="w-3.5 h-3.5" />
          <span>{t('newDashboard')}</span>
        </DropdownMenuItem>}

        {!isAdminViewer && activeDashboard && (
          <>
            <DropdownMenuItem disabled={layoutOperationPending} onClick={props.onRename} className="text-xs gap-2">
              <Pencil className="w-3.5 h-3.5" />
              <span>{t('renameDashboard')}</span>
            </DropdownMenuItem>

            <DropdownMenuItem
              disabled={layoutOperationPending}
              onClick={props.onDelete}
              className="text-xs gap-2 text-destructive focus:text-destructive"
            >
              <Trash2 className="w-3.5 h-3.5" />
              <span>{t('deleteDashboard')}</span>
            </DropdownMenuItem>
          </>
        )}

        {!isAdminViewer && <DropdownMenuSeparator />}

        {!isAdminViewer && <DropdownMenuItem disabled={layoutOperationPending} onClick={props.onOpenPresets} className="text-xs gap-2">
          <Sparkles className="w-3.5 h-3.5 text-muted-foreground" />
          <span>{t('presets')}</span>
        </DropdownMenuItem>}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Props of the header summary and action row. */
interface HeaderProps extends SwitcherProps {
  /** Pre-joined "updated · stale" summary, or null when there is nothing to say. */
  summary: string | null;
  isOwner: boolean;
  isEditMode: boolean;
  canEditLayout: boolean;
  onToggleEdit: () => void;
  onOpenManagement: () => void;
}

/**
 * Page header (Dashboard artboard): switcher-as-title, freshness summary and the edit / manage actions.
 */
export function DashboardHeader(props: HeaderProps) {
  const t = useTranslations('dashboard');
  const { summary, isOwner, isAdminViewer, isEditMode, canEditLayout, layoutOperationPending } = props;
  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="mr-auto flex min-w-0 basis-full flex-col gap-0.5 sm:basis-auto">
        <h1 className="m-0 min-w-0 text-2xl font-semibold leading-tight tracking-[-0.02em] text-foreground">
          <DashboardSwitcher {...props} />
        </h1>
        {summary && <p className="m-0 text-sm text-muted-foreground">{summary}</p>}
      </div>
      {!isAdminViewer && <Button
        type="button"
        variant={isEditMode ? 'default' : 'outline'}
        className="font-semibold"
        disabled={layoutOperationPending || (!canEditLayout && !isEditMode)}
        onClick={props.onToggleEdit}
      >
        {isEditMode ? <Eye className="w-4 h-4" aria-hidden="true" /> : <Edit3 className="w-4 h-4" aria-hidden="true" />}
        <span>{isEditMode ? t('finishEditing') : t('toggleEdit')}</span>
      </Button>}
      {isOwner && <Button type="button" variant="outline" onClick={props.onOpenManagement}>
        {t('manageTopicsAndHighlights')}
      </Button>}
    </div>
  );
}
