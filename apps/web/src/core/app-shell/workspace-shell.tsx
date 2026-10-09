'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { createContext, useCallback, useContext, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { MessageSquareIcon } from 'lucide-react';
import { UmweltLogo } from '@/components/brand/umwelt-mark';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { CommandPalette } from '@/core/command-palette';
import { ConnectionFooter } from '@/core/app-shell/connection-footer';
import { useChatController } from '@/core/app-shell/chat-controller';
import { SettingsNav, SourcesSubNav } from '@/core/app-shell/settings-nav';
import { destinationEnabled, mainNavigation, settingsGroups, type ModuleAvailability } from '@/core/module-registry';
import { useDisplayPreferences } from '@/core/query-provider';
import { useRealtime } from '@/core/realtime-provider';
import { ChangePasswordDialog } from '@/modules/account/change-password-dialog';
import { GoogleLink } from '@/modules/account/google-link';
import { UserMenu } from '@/modules/account/user-menu';
import { OwnerPreferences, PreferencesDialog } from '@/modules/account/preferences-dialog';
import { ChatDrawer } from '@/modules/chat/chat-drawer';

type Session = { authenticated: true; csrfToken: string };

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
 * Button that toggles the quick-chat Sheet drawer presentation and listens for keyboard shortcut (Cmd+J / Ctrl+J).
 *
 * @returns Accessible button triggering chat drawer.
 */
function ChatTriggerButton() {
  const t = useTranslations('chat');
  const chatCtrl = useChatController();

  useEffect(() => {
    /** Global shortcut Ctrl+J or Cmd+J to toggle chat drawer */
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'j') {
        e.preventDefault();
        chatCtrl.toggleDrawer();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [chatCtrl]);

  return (
    <button
      type="button"
      onClick={chatCtrl.toggleDrawer}
      className="button secondary flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold"
      title={t('quickChat')}
      aria-label={t('quickChat')}
    >
      <MessageSquareIcon className="size-4 text-primary" />
      <span className="hidden sm:inline">{t('quickChat')}</span>
    </button>
  );
}

/** Owns workspace session loading, sign-out, preference reload, and the shared navigation shell for child routes. */
export function WorkspaceShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const client = useQueryClient();
  const t = useTranslations('shell');
  const display = useDisplayPreferences();
  const realtime = useRealtime();
  const online = useBrowserOnline();
  const [accountOpen, setAccountOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const menuTriggerRef = useRef<HTMLButtonElement>(null);
  const sessionEndedRef = useRef(false);
  /** Set while the owner's own logout is in flight: the server has revoked the session, so 401s are expected. */
  const signingOutRef = useRef(false);
  const session = useQuery({ queryKey: ['session'], queryFn: () => apiRequest<Session>('/api/v1/auth/session') });
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
    router.replace(expired && !signingOutRef.current ? '/login?reason=expired' : '/login');
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

  const logout = useMutation({
    mutationFn: () => {
      signingOutRef.current = true;
      return apiRequest<void>('/api/v1/auth/logout', {
        method: 'POST',
        headers: csrfHeaders(session.data?.csrfToken ?? ''),
      });
    },
    onSuccess: () => endSession(),
    onError: () => { signingOutRef.current = false; },
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

  if (session.isPending) return <main className="shell"><div className="status-panel skeleton" aria-label={t('loadingWorkspace')} /></main>;
  if (session.isError || !session.data) {
    const expired = session.error instanceof ApiError && session.error.status === 401;
    return <><main id="main-content" className="page"><section className="status-panel">
      <h1>{expired ? t('expiredTitle') : t('workspaceUnavailable')}</h1>
      <p className="muted">{expired ? t('expiredHelp') : t('workspaceUnavailableHelp')}</p>
      <Button className="secondary" onClick={() => session.refetch()}>{t('retry')}</Button>
    </section></main><ConnectionFooter apiStatus={expired ? 'expired' : online ? 'unavailable' : 'clientOffline'} realtimeStatus={realtime.status} onRetry={() => apiHealth.refetch()} retrying={apiHealth.isFetching} /></>;
  }

  const savedPreferences = preferences.data ?? display.confirmedPreferences ?? null;
  const visibleMainNavigation = mainNavigation.filter((item) => destinationEnabled(item, moduleAvailability.data));
  const visibleSettingsGroups = settingsGroups.filter((item) => destinationEnabled(item, moduleAvailability.data));
  const settingsActive = pathname.startsWith('/settings');
  const active = visibleMainNavigation.find((item) => item.id !== 'settings' && (pathname === item.href || pathname.startsWith(`${item.href}/`)))?.id ?? (settingsActive ? 'settings' : '');
  /** Returns focus to the triggering menu control after a dialog closes. */
  const restoreMenuFocus = (event: Event) => {
    event.preventDefault();
    menuTriggerRef.current?.focus();
  };

  return <SessionContext.Provider value={session.data}>
      <div className="shell">
        <a href="#main-content" className="skip-link">{t('skipToContent')}</a>
        <header className="topbar">
          <Link href="/app" className="brand-name" aria-label="Umwelt-OS"><UmweltLogo /></Link>
          <div className="top-actions">
            {visibleMainNavigation.some((item) => item.id === 'chat') && <ChatTriggerButton />}
            <CommandPalette />
            <UserMenu triggerRef={menuTriggerRef} onOpenPreferences={() => setPreferencesOpen(true)} onOpenAccount={() => setAccountOpen(true)} onOpenChangePassword={() => setPasswordOpen(true)} onSignOut={() => logout.mutate()} signOutPending={logout.isPending} />
          </div>
        </header>
        <div className="workspace">
          <nav className="workspace-nav" aria-label={t('mainNavigation')}>
            {visibleMainNavigation.map((item) => <Link key={item.id} href={item.href} aria-current={active === item.id ? 'page' : undefined}>{t(item.messageKey)}</Link>)}
          </nav>
          {settingsActive && <SettingsNav groups={visibleSettingsGroups} pathname={pathname} />}
          <main id="main-content" tabIndex={-1} className="workspace-main">{pathname.startsWith('/settings/sources') && <SourcesSubNav pathname={pathname} />}{children}</main>
        </div>
        <ConnectionFooter
          apiStatus={!online ? 'clientOffline' : apiHealth.isFetching && apiHealth.isError ? 'reconnecting' : apiHealth.isPending ? 'connecting' : apiHealth.isError ? 'unavailable' : 'connected'}
          realtimeStatus={realtime.status}
          onRetry={() => apiHealth.refetch()}
          retrying={apiHealth.isFetching}
        />
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
          </DialogContent>
        </Dialog>
        <ChangePasswordDialog open={passwordOpen} onOpenChange={setPasswordOpen} csrfToken={session.data.csrfToken} closeLabel={t('close')} onCloseAutoFocus={restoreMenuFocus} />
        <ChatDrawer />
      </div>
  </SessionContext.Provider>;
}
