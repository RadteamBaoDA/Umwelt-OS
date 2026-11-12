'use client';

import { useQuery } from '@tanstack/react-query';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useWorkspace } from '@/core/workspace-context';
import { listAllSources, sourceKeys, type Source } from '@/modules/sources';
import { dashboardKeys, getDashboard, listDashboards, type Dashboard } from './api';

/** Management dialog state (topics / highlight rules) shared by the header button, first-run panel and deep link. */
export interface ManagementDialogState {
  open: boolean;
  setOpen: (open: boolean) => void;
  tab: 'topics' | 'rules';
  setTab: (tab: 'topics' | 'rules') => void;
}

/**
 * Owns the topics/highlights management dialog state and the legacy `?manage=highlights` deep link.
 *
 * The dialog opens exactly once on the false -> true transition of `manageLinkReady` (state is adjusted while
 * rendering, not in an effect); a separate effect then removes the one-shot `manage` query flag from the URL.
 *
 * @param manageLinkReady True once the owner's dashboard list resolved to the addressed dashboard with the deep link present.
 * @returns Dialog open flag, selected tab and their setters.
 */
export function useManagementDialog(manageLinkReady: boolean): ManagementDialogState {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<'topics' | 'rules'>('topics');
  // Adjust state while rendering (not in an effect) on the false -> true transition, so the dialog opens exactly once.
  const [manageLinkSeen, setManageLinkSeen] = useState(false);
  if (manageLinkReady !== manageLinkSeen) {
    setManageLinkSeen(manageLinkReady);
    if (manageLinkReady) {
      setTab('rules');
      setOpen(true);
    }
  }

  /** Removes the one-shot `manage` query flag once the deep link has opened highlight management. */
  useEffect(() => {
    if (!manageLinkReady) return;
    const params = new URLSearchParams(searchParams.toString());
    params.delete('manage');
    router.replace(params.size ? `${pathname}?${params.toString()}` : pathname, { scroll: false });
  }, [manageLinkReady, pathname, router, searchParams]);

  return { open, setOpen, tab, setTab };
}

/**
 * Redirects to the first dashboard when the URL addresses a missing dashboard or the bare `/dashboard` route.
 * Side effect: `router.replace` once the dashboard list has loaded; the query string is preserved.
 */
function useCanonicalDashboardRedirect(
  dashboardId: string | undefined,
  dashboards: { id: string }[],
  listLoaded: boolean,
): void {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  useEffect(() => {
    if (!listLoaded) return;
    const firstDashboardId = dashboards[0]?.id;
    const suffix = searchParams.size ? `?${searchParams.toString()}` : '';
    if (dashboardId && !dashboards.some((dashboard) => dashboard.id === dashboardId)) {
      router.replace(firstDashboardId ? `/dashboard/${encodeURIComponent(firstDashboardId)}${suffix}` : `/dashboard${suffix}`);
    } else if (pathname === '/dashboard' && firstDashboardId) {
      router.replace(`/dashboard/${encodeURIComponent(firstDashboardId)}${suffix}`);
    }
  }, [dashboardId, dashboards, listLoaded, pathname, router, searchParams]);
}

/** Source-derived dashboard data: stale sources, freshness strip sources, workspace sources and last update. */
export interface DashboardSources {
  /** Active sources read by this dashboard (freshness strip input). */
  dashboardSources: Source[];
  /** Active dashboard sources whose latest error is newer than their latest success. */
  staleSources: Source[];
  /** Workspace-wide source list query (first-run panel and Today counts). */
  allSources: { isSuccess: boolean; items: Source[] | undefined };
  /** Newest successful collection across sources in epoch ms, or null when unknown. */
  lastUpdatedAt: number | null;
}

/**
 * Loads the sources the active dashboard reads and derives stale/fresh summaries.
 *
 * @param activeDashboard The loaded dashboard, enabling the stale-source query once present.
 * @param isOwner Whether the viewer may read the workspace-wide source list.
 */
function useDashboardSources(activeDashboard: Dashboard | null, isOwner: boolean): DashboardSources {
  // Stale sources: sources this dashboard reads whose latest error is newer than the latest success.
  // One all-pages query serves the dashboard's sources and the owner's workspace-wide counts.
  const sourcesQuery = useQuery({
    queryKey: sourceKeys.listAll,
    queryFn: listAllSources,
    enabled: Boolean(activeDashboard) || isOwner,
  });
  const dashboardSourceIds = new Set(activeDashboard?.instances.flatMap((i) => i.definition.source_ids) ?? []);
  const dashboardSources = useMemo(
    () => (sourcesQuery.data?.items ?? []).filter((s) => dashboardSourceIds.has(s.id) && s.status === 'active'),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sourcesQuery.data, activeDashboard],
  );
  const staleSources = (sourcesQuery.data?.items ?? []).filter((s) =>
    dashboardSourceIds.has(s.id) && s.status === 'active' && s.last_error_at
    && (!s.last_success_at || Date.parse(s.last_error_at) > Date.parse(s.last_success_at)));
  /** Newest successful collection across sources, for the title summary; null when unknown. */
  const lastUpdatedAt = useMemo(() => {
    const times = (sourcesQuery.data?.items ?? []).map((s) => (s.last_success_at ? Date.parse(s.last_success_at) : NaN)).filter(Number.isFinite);
    return times.length ? Math.max(...times) : null;
  }, [sourcesQuery.data]);
  return {
    dashboardSources,
    staleSources,
    // Owner-only: observers get no workspace-wide list (the endpoint is not theirs to read).
    allSources: { isSuccess: isOwner && sourcesQuery.isSuccess, items: isOwner ? sourcesQuery.data?.items : undefined },
    lastUpdatedAt,
  };
}

/** True when the addressed dashboard exists, or (bare route) the workspace has no dashboards at all. */
function addressedDashboardResolved(dashboards: { id: string }[], dashboardId: string | undefined): boolean {
  return dashboardId ? dashboards.some((dashboard) => dashboard.id === dashboardId) : dashboards.length === 0;
}

/** Fetches the addressed dashboard's detail; disabled until a dashboard id is known. */
function useActiveDashboardQuery(selectedDashboardId: string | null) {
  return useQuery({
    queryKey: dashboardKeys.detail(selectedDashboardId || ''),
    queryFn: ({ signal }) => getDashboard(selectedDashboardId!, signal),
    enabled: Boolean(selectedDashboardId),
  });
}

/** Minimal query status used to decide route readiness. */
type QueryStatus = { isError: boolean; isSuccess: boolean };

/**
 * True once the dashboard list failed, or loaded and the addressed dashboard (if any) is known to exist and
 * its detail query settled. Exposed as `data-dashboard-ready` for tests and e2e waits.
 */
function isRouteReady(
  list: QueryStatus,
  detail: QueryStatus,
  dashboards: { id: string }[],
  dashboardId: string | undefined,
  selectedDashboardId: string | null,
): boolean {
  if (list.isError) return true;
  if (!list.isSuccess) return false;
  const requestedDashboardExists = !dashboardId || dashboards.some((dashboard) => dashboard.id === dashboardId);
  return dashboards.length === 0
    || requestedDashboardExists && (!selectedDashboardId || detail.isSuccess || detail.isError);
}

/**
 * Loads the dashboard list and the addressed dashboard, and owns route normalisation.
 *
 * The route owns dashboard selection; `/dashboard` resolves to the workspace's first dashboard. Effects run in the
 * order: legacy manage deep link cleanup, canonical redirect.
 *
 * @param dashboardId Dashboard identity from the canonical route, when present.
 * @returns Queries' derived data, source summaries, management dialog state and navigation helper.
 */
export function useDashboardData(dashboardId: string | undefined) {
  const { isOwner } = useWorkspace();
  const searchParams = useSearchParams();
  const router = useRouter();
  const dashboardsQuery = useQuery({
    queryKey: dashboardKeys.all,
    queryFn: ({ signal }) => listDashboards(signal),
  });
  const dashboards = useMemo(() => dashboardsQuery.data ?? [], [dashboardsQuery.data]);
  const selectedDashboardId = dashboardId ?? dashboards[0]?.id ?? null;
  // Legacy `?manage=highlights` deep link: ready once the owner's dashboard list resolved to the addressed dashboard.
  const manageLinkReady = searchParams.get('manage') === 'highlights' && isOwner && dashboardsQuery.isSuccess
    && addressedDashboardResolved(dashboards, dashboardId);
  const management = useManagementDialog(manageLinkReady);
  useCanonicalDashboardRedirect(dashboardId, dashboards, dashboardsQuery.isSuccess);

  /** Navigates to a dashboard's canonical URL after the caller's dirty-state guard succeeds. */
  const navigateToDashboard = useCallback((id: string, replace = false) => {
    const href = `/dashboard/${encodeURIComponent(id)}`;
    if (replace) router.replace(href);
    else router.push(href);
  }, [router]);

  const activeDashboardQuery = useActiveDashboardQuery(selectedDashboardId);
  const activeDashboard = activeDashboardQuery.data ?? null;
  const dashboardRouteReady = isRouteReady(dashboardsQuery, activeDashboardQuery, dashboards, dashboardId, selectedDashboardId);
  const sources = useDashboardSources(activeDashboard, isOwner);
  // A dashboard that already has gadgets is never hidden behind first-run setup.
  const showFirstRun = isOwner && sources.allSources.isSuccess && (sources.allSources.items?.length ?? 1) === 0
    && (activeDashboard?.instances.length ?? 0) === 0;

  return { dashboards, selectedDashboardId, activeDashboard, dashboardRouteReady, navigateToDashboard, management, sources, showFirstRun };
}
