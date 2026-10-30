'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { createContext, useCallback, useContext, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { LayoutDashboard, MessageSquare, Settings, type LucideIcon } from 'lucide-react';
import { UmweltLogo, UmweltMark } from '@/components/brand/umwelt-mark';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { ApiError, apiRequest, csrfHeaders, retrySessionConflict } from '@/core/api';
import { CommandPalette } from '@/core/command-palette';
import { ConnectionFooter } from '@/core/app-shell/connection-footer';
import { useChatController } from '@/core/app-shell/chat-controller';
import { SettingsPanel, SourcesSubNav, groupActive } from '@/core/app-shell/settings-nav';
import { WorkspaceSwitcher } from '@/core/app-shell/workspace-switcher';
import { useWorkspace } from '@/core/workspace-context';
import { destinationEnabled, destinationForRole, mainNavigation, settingsExtraLinks, settingsGroups, type ModuleAvailability } from '@/core/module-registry';
import { useDisplayPreferences } from '@/core/query-provider';
import { useRealtime } from '@/core/realtime-provider';
import { ChangePasswordDialog } from '@/modules/account/change-password-dialog';
import { GoogleLink } from '@/modules/account/google-link';
import { AccountDeletionSection } from '@/modules/account/account-deletion';
import { LegalLinks } from '@/modules/legal/legal-links';
import { UserMenu } from '@/modules/account/user-menu';
import { OwnerPreferences, PreferencesDialog } from '@/modules/account/preferences-dialog';
import { ChatDrawer } from '@/modules/chat/chat-drawer';
import { NotificationBell } from '@/modules/notifications/notification-bell';

type Session = { authenticated: true; csrfToken: string; isInstanceAdmin: boolean };

const navigationIcons: Record<string, LucideIcon> = {
  dashboard: LayoutDashboard,
  chat: MessageSquare,
  settings: Settings,
};

/** Browser connectivity (navigator.onLine); assumed online during server render. */
function useBrowserOnline() {
  return useSyncExternalStore(
    (notify) => { window.addEventListener('online', notify); window.addEventListener('offline', notify); return () => { window.removeEventListener('online', notify); window.removeEventListener('offline', notify); }; },
    () => navigator.onLine,
    () => true,
  );
}
const SessionContext = createContext<Session | null>(null);

/** Returns the authenticated workspace session, including its CSRF token; throws when used outside the session provider. */
export function useWorkspaceSession() {
  const session = useContext(SessionContext);
  if (!session) throw new Error('Workspace session is unavailable');
  return session;
}

/**
 * Button that toggles the quick-chat Sheet drawer and listens for Cmd+J / Ctrl+J.
 *
 * @returns Accessible button triggering chat drawer.
 */
function ChatTriggerButton() {
  const t = useTranslations('chat');
  const chatCtrl = useChatController();
  const { toggleDrawer } = chatCtrl;
  const pathname = usePathname();

  useEffect(() => {
    /** Global shortcut Ctrl+J or Cmd+J to toggle chat drawer without rebinding on draft changes. */
    const handleKeyDown = (e: KeyboardEvent) => {
      // The full Chat page is already the chat surface: opening a drawer over it would duplicate the session.
      if (pathname === '/chat' || pathname.startsWith('/chat/')) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'j') {
        e.preventDefault();
        toggleDrawer();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [toggleDrawer, pathname]);

  return (
    <Button
      type="button"
      variant="outline"
      onClick={toggleDrawer}
      className="text-sm font-semibold"
      title={t('quickChat')}
      aria-label={t('quickChat')}
    >
      <MessageSquare className="size-4 text-muted-foreground" aria-hidden="true" />
      <span className="hidden sm:inline">{t('quickChat')}</span>
    </Button>
  );
}

/**
 * Module-scope latch: true from the owner's logout request until the next successful session read.
 * It outlives the auth-keyed remount of the shell, whose fresh refs would otherwise report the post-logout 401
 * as `?reason=expired`.
 */
let ownerSignOutInFlight = false;

/**
 * Render the authenticated app shell with session, route, and responsive main-navigation state.
 * Navigation is a 56px icon rail (logo at its top, tooltip plus accessible name per destination) beside a 48px top bar;
 * at 720px and below the rail becomes the labelled bottom bar. The shell keeps the header,
 * navigation, and top-bar connection status fixed while `.workspace-main` owns page scrolling.
 * @param children The current route content rendered inside the main scroll region.
 */
export function WorkspaceShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const client = useQueryClient();
  const t = useTranslations('shell');
  const display = useDisplayPreferences();
  const realtime = useRealtime();
  const role = useWorkspace().selection?.role;
  const online = useBrowserOnline();
  const [accountOpen, setAccountOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const menuTriggerRef = useRef<HTMLButtonElement>(null);
  const sessionEndedRef = useRef(false);
  /** Set while the owner's own logout is in flight: the server has revoked the session, so 401s are expected. */
  const signingOutRef = useRef(false);
  const session = useQuery({
    queryKey: ['session'],
    queryFn: () => apiRequest<Session>('/api/v1/auth/session'),
    // The server answers 409 ("retry the request") when concurrent tabs/requests hold its NOWAIT owner lock while the
    // CSRF token rotates; one transient conflict must not turn the whole shell into "Workspace is unavailable".
    retry: retrySessionConflict,
    retryDelay: (attempt) => 150 * (attempt + 1),
  });
  const moduleAvailability = useQuery({
    queryKey: ['module-lifecycle'],
    queryFn: () => apiRequest<ModuleAvailability>('/api/v1/settings/modules'),
    refetchOnWindowFocus: true,
  });
  const generation = display.authGeneration;
  const preferences = useQuery({
    queryKey: ['owner-preferences'],
    enabled: Boolean(session.data),
    queryFn: async ({ signal }) => {
      const value = await apiRequest<OwnerPreferences>('/api/v1/settings/preferences', { signal });
      if (!display.isCurrentGeneration(generation)) throw new Error('Stale preference response');
      return value;
    },
  });
  const apiHealth = useQuery({ queryKey: ['shell-api-health'], queryFn: () => apiRequest<unknown>('/health'), refetchInterval: 30_000 });

  useEffect(() => {
    if (session.data && preferences.data && display.isCurrentGeneration(generation)) {
      display.confirmPreferences(preferences.data, generation);
    }
  }, [session.data, preferences.data, generation, display.confirmPreferences, display.isCurrentGeneration]);

  /** Clears authenticated workspace state and ends the current client session. */
  const endSession = useCallback((expired = false) => {
    if (sessionEndedRef.current) return;
    sessionEndedRef.current = true;
    window.dispatchEvent(new Event('bbd:auth-ending'));
    void client.cancelQueries();
    client.clear();
    display.endAuthSession();
    setPreferencesOpen(false);
    setAccountOpen(false);
    // Auth-keyed remounts create a fresh shell with signingOutRef=false; the module latch keeps the owner's
    // sign-out from being re-reported as an expiry by that shell's own 401.
    router.replace(expired && !signingOutRef.current && !ownerSignOutInFlight ? '/login?reason=expired' : '/login');
  }, [client, display.endAuthSession, router]);

  useEffect(() => {
    /** Routes an unauthorized event through the shared session-ending handler. */
    const unauthorized = () => endSession(true);
    /** Updates the cached session from the detail of a session-refresh event. */
    const refreshed = (event: Event) => client.setQueryData(['session'], (event as CustomEvent<Session>).detail);
    window.addEventListener('bbd:unauthorized', unauthorized);
    window.addEventListener('bbd:session-refreshed', refreshed);
    return () => {
      window.removeEventListener('bbd:unauthorized', unauthorized);
      window.removeEventListener('bbd:session-refreshed', refreshed);
    };
  }, [client, endSession]);

  useEffect(() => {
    if (session.error instanceof ApiError && session.error.status === 401) endSession(true);
  }, [endSession, session.error]);

  useEffect(() => {
    // A successful session read proves a new login: later 401s are genuine expiries again.
    // The ended shell is excluded: it can still re-emit its stale cached session after client.clear().
    if (session.data && !sessionEndedRef.current) ownerSignOutInFlight = false;
  }, [session.data]);

  const logout = useMutation({
    mutationFn: () => {
      signingOutRef.current = true;
      ownerSignOutInFlight = true;
      return apiRequest<void>('/api/v1/auth/logout', {
        method: 'POST',
        headers: csrfHeaders(session.data?.csrfToken ?? ''),
      });
    },
    onSuccess: () => endSession(),
    onError: () => { signingOutRef.current = false; ownerSignOutInFlight = false; },
  });

  /** Loads owner preferences for the dialog retry; returns null on request failure, abort, or stale auth generation. */
  const reloadPreferences = useCallback(async (signal: AbortSignal) => {
    try {
      const value = await apiRequest<OwnerPreferences>('/api/v1/settings/preferences', { signal });
      return signal.aborted || !display.isCurrentGeneration(generation) ? null : value;
    } catch {
      return null;
    }
  }, [display.isCurrentGeneration, generation]);

  const visibleSettingsGroups = settingsGroups.filter((item) => destinationEnabled(item, moduleAvailability.data) && destinationForRole(item, role));
  // A member deep-linking to an owner settings page is sent to the first settings group they may see.
  const memberSettingsRedirect = role === 'member' && pathname.startsWith('/settings') && !visibleSettingsGroups.some((g) => groupActive(pathname, g.href)) ? visibleSettingsGroups[0]?.href : undefined;
  const adminViewerSettingsRedirect = role === 'admin_viewer' && pathname.startsWith('/settings') && pathname !== '/settings/global'
    ? session.data?.isInstanceAdmin ? '/settings/global' : '/dashboard'
    : undefined;
  const adminViewerPrivateRedirect = role === 'admin_viewer' && (pathname === '/chat' || pathname.startsWith('/chat/') || pathname.startsWith('/knowledge/memory'))
    ? '/dashboard'
    : undefined;
  useEffect(() => {
    const redirect = memberSettingsRedirect ?? adminViewerSettingsRedirect ?? adminViewerPrivateRedirect;
    if (redirect) router.replace(redirect);
  }, [adminViewerPrivateRedirect, adminViewerSettingsRedirect, memberSettingsRedirect, router]);

  if (session.isPending) return <div className="shell">
    <header className="topbar"><Link href="/login" className="brand-name" aria-label="Umwelt-OS"><UmweltLogo /></Link><ConnectionFooter apiStatus="connecting" realtimeStatus={realtime.status} onRetry={() => apiHealth.refetch()} retrying={apiHealth.isFetching} /></header>
    <main id="main-content" tabIndex={-1} className="page"><div className="status-panel skeleton" aria-label={t('loadingWorkspace')} /></main>
  </div>;
  if (session.isError || !session.data) {
    const expired = session.error instanceof ApiError && session.error.status === 401;
    return <div className="shell">
      <header className="topbar"><Link href="/login" className="brand-name" aria-label="Umwelt-OS"><UmweltLogo /></Link><ConnectionFooter apiStatus={expired ? 'expired' : online ? 'unavailable' : 'clientOffline'} realtimeStatus={realtime.status} onRetry={() => apiHealth.refetch()} retrying={apiHealth.isFetching} /></header>
      <main id="main-content" tabIndex={-1} className="page"><section className="status-panel">
      <h1>{expired ? t('expiredTitle') : t('workspaceUnavailable')}</h1>
      <p className="text-muted-foreground">{expired ? t('expiredHelp') : t('workspaceUnavailableHelp')}</p>
      <Button variant="outline" onClick={() => session.refetch()}>{t('retry')}</Button>
      </section></main>
    </div>;
  }

  const savedPreferences = preferences.data ?? display.confirmedPreferences ?? null;
  // Members see only the read-only translation settings group; their Settings entry opens it directly.
  const visibleMainNavigation = mainNavigation.filter((item) => destinationEnabled(item, moduleAvailability.data)
    && (destinationForRole(item, role) || item.id === 'settings' && session.data.isInstanceAdmin)
    && !(role === 'admin_viewer' && item.id === 'chat')
    && (item.id !== 'settings' || visibleSettingsGroups.length > 0 || session.data.isInstanceAdmin));
  const settingsActive = pathname.startsWith('/settings');
  // A member deep-linking to an owner settings page is sent to the first group they may see.
  const active = visibleMainNavigation.find((item) => item.id !== 'settings' && ((item.id === 'dashboard' && pathname.startsWith('/dashboard/')) || pathname === item.href || pathname.startsWith(`${item.href}/`)))?.id ?? (settingsActive ? 'settings' : '');
  /** Returns focus to the triggering menu control after a dialog closes. */
  const restoreMenuFocus = (event: Event) => {
    event.preventDefault();
    menuTriggerRef.current?.focus();
  };

  return <SessionContext.Provider value={session.data}>
      <div className="shell">
        <a href="#main-content" className="skip-link">{t('skipToContent')}</a>
        <div className="workspace">
          <TooltipProvider delayDuration={300}>
            <nav id="workspace-nav" className="workspace-nav" aria-label={t('mainNavigation')}>
              <div className="workspace-nav-header">
                <Link href="/dashboard" className="brand-name workspace-brand" aria-label="Umwelt-OS"><UmweltMark /></Link>
              </div>
              <div className="workspace-nav-links">
                {visibleMainNavigation.map((item) => {
                  const Icon = navigationIcons[item.id];
                  const link = <Link
                    key={item.id}
                    href={item.id === 'dashboard' && pathname.startsWith('/dashboard/') ? pathname : item.id === 'settings' && role === 'admin_viewer' ? '/settings/global' : item.id === 'settings' && role === 'member' ? visibleSettingsGroups[0].href : item.href}
                    aria-current={active === item.id ? 'page' : undefined}
                    className="workspace-nav-link"
                  >
                    <Icon className="size-[18px] shrink-0" aria-hidden="true" />
                    <span className="workspace-nav-label">{t(item.messageKey)}</span>
                  </Link>;
                  return <Tooltip key={item.id}>
                    <TooltipTrigger asChild>{link}</TooltipTrigger>
                    <TooltipContent className="workspace-nav-tooltip" side="right">{t(item.messageKey)}</TooltipContent>
                  </Tooltip>;
                })}
              </div>
            </nav>
          </TooltipProvider>
          <div className="workspace-column">
          <header className="topbar">
            <ConnectionFooter
              apiStatus={!online ? 'clientOffline' : apiHealth.isFetching && apiHealth.isError ? 'reconnecting' : apiHealth.isPending ? 'connecting' : apiHealth.isError ? 'unavailable' : 'connected'}
              realtimeStatus={realtime.status}
              onRetry={() => apiHealth.refetch()}
              retrying={apiHealth.isFetching}
            />
            <div className="top-actions">
              {pathname.startsWith('/dashboard/') && visibleMainNavigation.some((item) => item.id === 'chat') && <ChatTriggerButton />}
              <CommandPalette />
              <NotificationBell />
              <WorkspaceSwitcher />
              <UserMenu triggerRef={menuTriggerRef} onOpenPreferences={() => setPreferencesOpen(true)} onOpenAccount={() => setAccountOpen(true)} onOpenChangePassword={() => setPasswordOpen(true)} onSignOut={() => logout.mutate()} signOutPending={logout.isPending} />
            </div>
          </header>
          <main id="main-content" tabIndex={-1} className="workspace-main" data-workspace-readonly={role === 'admin_viewer' || undefined}>{memberSettingsRedirect || adminViewerSettingsRedirect ? null : settingsActive
            ? <SettingsPanel links={[...visibleSettingsGroups, ...settingsExtraLinks.filter((item) => item.adminOnly ? session.data.isInstanceAdmin : destinationForRole(item, role))]} pathname={pathname}>{pathname.startsWith('/settings/sources') && <SourcesSubNav pathname={pathname} />}{children}</SettingsPanel>
            : children}</main>
          </div>
        </div>
        {logout.error && <p className="error" role="alert">{t('signOutFailed')}</p>}
        <PreferencesDialog
          open={preferencesOpen}
          onOpenChange={setPreferencesOpen}
          preferences={savedPreferences}
          loading={preferences.isPending && !preferences.data}
          loadError={preferences.isError && !preferences.data}
          retrying={preferences.isFetching}
          onRetry={reloadPreferences}
          csrfToken={session.data.csrfToken}
          savingDisabled={!preferences.data}
          authGeneration={generation}
          onCloseAutoFocus={restoreMenuFocus}
        />
        <Dialog open={accountOpen} onOpenChange={setAccountOpen}>
          <DialogContent closeLabel={t('close')} onCloseAutoFocus={restoreMenuFocus}>
            <DialogHeader><DialogTitle>{t('accountSettings')}</DialogTitle><DialogDescription>{t('googleAccountDescription')}</DialogDescription></DialogHeader>
            <GoogleLink />
            <AccountDeletionSection csrfToken={session.data.csrfToken} />
            <LegalLinks />
          </DialogContent>
        </Dialog>
        <ChangePasswordDialog open={passwordOpen} onOpenChange={setPasswordOpen} csrfToken={session.data.csrfToken} closeLabel={t('close')} onCloseAutoFocus={restoreMenuFocus} />
        {role !== 'admin_viewer' && <ChatDrawer />}
      </div>
  </SessionContext.Provider>;
}
