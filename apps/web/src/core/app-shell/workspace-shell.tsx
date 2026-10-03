'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { MessageSquareIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { CommandPalette } from '@/core/command-palette';
import { ConnectionFooter } from '@/core/app-shell/connection-footer';
import { ChatControllerProvider, useChatController } from '@/core/app-shell/chat-controller';
import { detailDestinations, mainNavigation, settingsGroups } from '@/core/module-registry';
import { useDisplayPreferences } from '@/core/query-provider';
import { useRealtime } from '@/core/realtime-provider';
import { GoogleLink } from '@/modules/account/google-link';
import { OwnerPreferences, PreferencesDialog } from '@/modules/account/preferences-dialog';
import { ChatDrawer } from '@/modules/chat/chat-drawer';

type Session = { authenticated: true; csrfToken: string };
const SessionContext = createContext<Session | null>(null);

/** Returns the authenticated workspace session, including its CSRF token; throws when used outside the session provider. */
export function useWorkspaceSession() {
  const session = useContext(SessionContext);
  if (!session) throw new Error('Workspace session is unavailable');
  return session;
}

/** Owns workspace session loading, sign-out, preference reload, and the shared navigation shell for child routes. */
export function WorkspaceShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const client = useQueryClient();
  const t = useTranslations('shell');
  const display = useDisplayPreferences();
  const realtime = useRealtime();
  const [accountOpen, setAccountOpen] = useState(false);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuTriggerRef = useRef<HTMLButtonElement>(null);
  const suppressMenuFocusRef = useRef(false);
  const sessionEndedRef = useRef(false);
  const session = useQuery({ queryKey: ['session'], queryFn: () => apiRequest<Session>('/api/v1/auth/session') });
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
  const endSession = useCallback(() => {
    if (sessionEndedRef.current) return;
    sessionEndedRef.current = true;
    window.dispatchEvent(new Event('bbd:auth-ending'));
    void client.cancelQueries();
    client.clear();
    display.endAuthSession();
    setPreferencesOpen(false);
    setAccountOpen(false);
    router.replace('/login');
  }, [client, display.endAuthSession, router]);

  useEffect(() => {
    /** Routes an unauthorized event through the shared session-ending handler. */
    const unauthorized = () => endSession();
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
    if (session.error instanceof ApiError && session.error.status === 401) endSession();
  }, [endSession, session.error]);

  const logout = useMutation({
    mutationFn: () => apiRequest<void>('/api/v1/auth/logout', {
      method: 'POST',
      headers: csrfHeaders(session.data?.csrfToken ?? ''),
    }),
    onSuccess: endSession,
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
    return <><main className="page"><section className="status-panel">
      <h1>{expired ? t('expiredTitle') : t('workspaceUnavailable')}</h1>
      <p className="muted">{expired ? t('expiredHelp') : t('workspaceUnavailableHelp')}</p>
      <Button className="secondary" onClick={() => session.refetch()}>{t('retry')}</Button>
    </section></main><ConnectionFooter apiStatus={expired ? 'expired' : 'unavailable'} realtimeStatus={realtime.status} onRetry={() => apiHealth.refetch()} retrying={apiHealth.isFetching} /></>;
  }

  const savedPreferences = preferences.data ?? display.confirmedPreferences ?? null;
  const settingsActive = pathname.startsWith('/settings');
  const active = mainNavigation.find((item) => item.id !== 'settings' && (pathname === item.href || pathname.startsWith(`${item.href}/`)))?.id ?? (settingsActive ? 'settings' : '');
  /** Opens the account or preferences surface selected by the menu action. */
  const openFromMenu = (dialog: 'account' | 'preferences') => {
    suppressMenuFocusRef.current = true;
    if (dialog === 'account') setAccountOpen(true);
    else setPreferencesOpen(true);
  };
  /** Returns focus to the triggering menu control after a dialog closes. */
  const restoreMenuFocus = (event: Event) => {
    event.preventDefault();
    menuTriggerRef.current?.focus();
  };

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
      <MessageSquareIcon className="size-4 text-accent" />
      <span className="hidden sm:inline">{t('quickChat')}</span>
    </button>
  );
}

  return <SessionContext.Provider value={session.data}>
    <ChatControllerProvider>
      <div className="shell">
        <header className="topbar">
          <Link href="/app" className="brand-name">BBD-OS</Link>
          <div className="top-actions">
            <ChatTriggerButton />
            <CommandPalette />
            <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
              <DropdownMenuTrigger asChild>
                <button ref={menuTriggerRef} type="button" className="button secondary" aria-label={t('userMenu')}>☻</button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" onCloseAutoFocus={(event) => {
                if (suppressMenuFocusRef.current) {
                  event.preventDefault();
                  suppressMenuFocusRef.current = false;
                }
              }}>
                <DropdownMenuItem onSelect={() => openFromMenu('preferences')}>{t('userSettings')}</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => openFromMenu('account')}>{t('accountSettings')}</DropdownMenuItem>
                <DropdownMenuItem disabled={logout.isPending} onSelect={() => logout.mutate()}>{t('signOut')}</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </header>
        <div className="workspace">
          <nav className="workspace-nav" aria-label={t('mainNavigation')}>
            {mainNavigation.map((item) => <Link key={item.id} href={item.href} aria-current={active === item.id ? 'page' : undefined}>{t(item.messageKey)}</Link>)}
          </nav>
          {settingsActive && <nav className="settings-nav" aria-label={t('settings')}>
            <h2>{t('settings')}</h2>
            {settingsGroups.map((item) => <Link key={item.id} href={item.href} aria-current={pathname === item.href ? 'page' : undefined}>{t(item.messageKey)}</Link>)}
            <div className="settings-details">{detailDestinations.map((item) => <Link key={item.id} href={item.href}>{t(item.messageKey)}</Link>)}</div>
          </nav>}
          <main className="workspace-main">{children}</main>
        </div>
        <ConnectionFooter
          apiStatus={apiHealth.isFetching && apiHealth.isError ? 'reconnecting' : apiHealth.isPending ? 'connecting' : apiHealth.isError ? 'unavailable' : 'connected'}
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
        <ChatDrawer />
      </div>
    </ChatControllerProvider>
  </SessionContext.Provider>;
}
