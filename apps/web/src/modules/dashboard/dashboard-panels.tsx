'use client';

import { Plus, Sparkles, X } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import React from 'react';
import { BareButton } from '@/components/bare-button';
import { Button } from '@/components/ui/button';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { formatDateTime, formatTimeZoneLabel } from '@/core/i18n';
import type { AppLocaleId } from '@/core/i18n';
import type { Source } from '@/modules/sources';
import type { DashboardGroup } from './api';
import { TodayHeader } from './today-header';
import type { LayoutEditOrigin } from './layout-geometry';

/** Group filter tabs; rendered by the page only when the dashboard has more than one group. */
export function GroupTabs({ groups, activeGroupId, onChange }: {
  groups: DashboardGroup[];
  activeGroupId: string;
  onChange: (id: string) => void;
}) {
  const t = useTranslations('dashboard');
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-border pb-2">
      <Tabs value={activeGroupId} onValueChange={onChange} className="h-8">
        <TabsList className="h-8 p-0.5 bg-muted/30">
          <TabsTrigger value="all" className="text-xs px-2.5 h-7">{t('allGroups')}</TabsTrigger>
          {groups.map((group) => (
            <TabsTrigger key={group.id} value={group.id} className="text-xs px-2.5 h-7">{group.name}</TabsTrigger>
          ))}
        </TabsList>
      </Tabs>
    </div>
  );
}

/** Status banners above the grid: frozen-origin notice, unavailable mobile layout and the dismissible mobile hint. */
export function LayoutNotices({ editOrigin, selectedDashboardId, requestedBreakpoint, mobileProjectionUnavailable, showMobileHint, onDismissMobileHint }: {
  editOrigin: LayoutEditOrigin | null;
  selectedDashboardId: string | null;
  requestedBreakpoint: 'desktop' | 'mobile';
  mobileProjectionUnavailable: boolean;
  showMobileHint: boolean;
  onDismissMobileHint: () => void;
}) {
  const t = useTranslations('dashboard');
  const frozen = editOrigin && editOrigin.dashboardId === selectedDashboardId && requestedBreakpoint !== editOrigin.breakpoint;
  return (
    <>
      {frozen && (
        <p role="status" className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-sm text-muted-foreground">
          {t('mobileEditOrigin', {
            breakpoint: t(editOrigin.breakpoint === 'mobile' ? 'mobileLayout' : 'desktopLayout'),
            requested: t(requestedBreakpoint === 'mobile' ? 'mobileLayout' : 'desktopLayout'),
          })}
        </p>
      )}

      {mobileProjectionUnavailable && (
        <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {t('mobileLayoutUnavailable')}
        </p>
      )}

      {showMobileHint && (
        <p role="status" className="flex items-start gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-sm text-muted-foreground">
          <span className="min-w-0 flex-1">{t('mobileReadingProjection')}</span>
          <BareButton type="button" aria-label={t('dismissNotice')} onClick={onDismissMobileHint} className="shrink-0 rounded p-0.5 hover:bg-primary/10">
            <X className="size-4" aria-hidden="true" />
          </BareButton>
        </p>
      )}
    </>
  );
}

/**
 * Dashed empty-state card. `variant="none"` is shown when the workspace has no dashboards; `"empty"` when the
 * dashboard (or selected group) has no gadgets. Actions are hidden from read-only administrators.
 */
export function DashboardEmptyState({ variant, isAdminViewer, canEditLayout, onOpenPresets, onCreateDashboard, onStartEmpty }: {
  variant: 'none' | 'empty';
  isAdminViewer: boolean;
  canEditLayout: boolean;
  onOpenPresets: () => void;
  onCreateDashboard: () => void;
  onStartEmpty: () => void;
}) {
  const t = useTranslations('dashboard');
  const none = variant === 'none';
  return (
    <section className="dashboard-empty flex flex-wrap items-center gap-6 rounded-lg border border-dashed border-border bg-card p-8">
      <div className="min-w-0 flex-[1_1_360px]">
        <h2 className="m-0 mb-1 text-xl font-semibold leading-snug text-foreground">{none ? t('noDashboardsYet') : t('emptyTitle')}</h2>
        <p className="m-0 text-sm text-muted-foreground">{none ? t('noDashboardsDesc') : t('emptyDesc')}</p>
      </div>
      {!isAdminViewer && none && <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" onClick={onOpenPresets}>
          <Sparkles className="size-4" aria-hidden="true" />
          {t('presets')}
        </Button>
        <Button type="button" className="font-semibold" onClick={onCreateDashboard}>
          <Plus className="size-4" aria-hidden="true" />
          {t('createFirstDashboard')}
        </Button>
      </div>}
      {!isAdminViewer && !none && <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" disabled={!canEditLayout} onClick={onStartEmpty}>
          {t('startEmpty')}
        </Button>
        <Button type="button" className="font-semibold" onClick={onOpenPresets}>
          {t('choosePreset')}
        </Button>
      </div>}
    </section>
  );
}

/** Collapsible list of sources whose latest error is newer than their latest success. */
export function StaleSourcesPanel({ sources, open, onToggle, locale, timezone }: {
  sources: Source[];
  open: boolean;
  onToggle: () => void;
  locale: AppLocaleId;
  timezone: string;
}) {
  const t = useTranslations('dashboard');
  return (
    <section aria-label={t('staleSourcesTitle')} className="space-y-2 rounded-md border border-border bg-card px-3 py-1 text-sm">
      {/* One summary line; the per-source detail list is a disclosure so it never dominates the page. */}
      <div className="flex flex-wrap items-center gap-x-2">
        <h2 className="text-sm font-semibold">{t('staleSourcesSummary', { count: sources.length })}</h2>
        <span aria-hidden="true" className="text-muted-foreground">·</span>
        <Link href="/settings/sources" className="inline-flex min-h-9 pointer-coarse:min-h-11 items-center text-xs font-semibold text-primary underline underline-offset-4">
          {t('staleSourcesReview')}
        </Link>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="ml-auto"
          aria-expanded={open}
          aria-controls="stale-sources-details"
          onClick={onToggle}
        >
          {t('staleSourcesDetails')}
        </Button>
      </div>
      {open && (
        <ul id="stale-sources-details" className="space-y-2 pb-2">
          {sources.map((s) => (
            <li key={s.id} className="space-y-0.5">
              <p>{t('staleSourceLine', { name: s.name, code: s.last_error_code ?? s.collection_error_code ?? s.processing_error_code ?? '—' })}</p>
              <p className="text-xs text-muted-foreground">
                {t('staleSourceTimes', {
                  success: s.last_success_at ? formatDateTime(s.last_success_at, locale, timezone) : t('staleSourceNever'),
                  error: formatDateTime(s.last_error_at!, locale, timezone),
                })}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Footer line stating the display timezone and linking to the source status page. */
export function TimezoneFooter({ locale, timezone }: { locale: AppLocaleId; timezone: string }) {
  const t = useTranslations('dashboard');
  return (
    <p className="flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
      <span>{t('timesShownIn', { timezone: formatTimeZoneLabel(timezone, locale) })}</span>
      <Link href="/settings/sources" className="inline-flex min-h-9 pointer-coarse:min-h-11 items-center text-foreground underline underline-offset-4">
        {t('openSourceStatus')}
      </Link>
    </p>
  );
}

/**
 * "Today" strip for owners once first-run setup is done: counts active sources and finds the Open-Meteo
 * weather source among them.
 */
export function TodayHeaderGate({ visible, sources }: { visible: boolean; sources: Source[] }) {
  if (!visible) return null;
  return (
    <TodayHeader
      sourceIds={sources.filter((s) => s.status === 'active').map((s) => s.id)}
      weatherSource={sources.find((s) => s.status === 'active' && s.provider === 'open_meteo')}
    />
  );
}
