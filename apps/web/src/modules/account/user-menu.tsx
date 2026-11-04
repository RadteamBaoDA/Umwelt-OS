'use client';

import Link from 'next/link';
import { useRef, useState, type RefObject } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { UserIcon } from 'lucide-react';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import type { OwnerPreferences } from '@/core/preferences';
import { useWorkspace } from '@/core/workspace-context';
import { NotificationItems, useNotificationFeed } from '@/modules/notifications/notification-bell';

/**
 * Avatar initials: first letter of the first and last words, upper-cased by code point (so "Đoàn Bảo" gives "ĐB").
 * A single word gives one letter; blank gives an empty string so the caller can fall back to the icon.
 */
export function initialsOf(name: string | null | undefined): string {
  const words = (name ?? '').trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return '';
  const first = (word: string) => Array.from(word)[0].toLocaleUpperCase();
  return words.length === 1 ? first(words[0]) : first(words[0]) + first(words[words.length - 1]);
}

export type UserMenuProps = {
  onOpenPreferences: () => void;
  onOpenAccount: () => void;
  onOpenChangePassword: () => void;
  onSignOut: () => void;
  signOutPending?: boolean;
  /** Lets the shell return focus to the avatar after the dialogs it opens close. */
  triggerRef?: RefObject<HTMLButtonElement | null>;
};

/**
 * Header avatar menu; owns its open state and focus return. Wave 5 extends the items, the shell only passes handlers.
 * Below 640px the header drops the workspace switcher and notification bell, so this menu carries them (`sm:hidden`).
 */
export function UserMenu({ onOpenPreferences, onOpenAccount, onOpenChangePassword, onSignOut, signOutPending, triggerRef }: UserMenuProps) {
  const t = useTranslations('shell');
  const a = useTranslations('account');
  const w = useTranslations('workspaces');
  const { workspaces, selection, selectWorkspace } = useWorkspace();
  // Below 640px the bell is hidden, so unread state surfaces as a dot on the avatar (shares the bell's cached query).
  const { unread } = useNotificationFeed();
  // Read-only subscription to the preferences the shell already loaded (never fetches here).
  const prefs = useQuery<OwnerPreferences>({ queryKey: ['owner-preferences'], enabled: false });
  const initials = initialsOf(prefs.data?.resolved_display_name ?? prefs.data?.display_name);
  const [open, setOpen] = useState(false);
  const suppressFocusRef = useRef(false);
  /** Runs a dialog-opening action without the menu stealing focus back on close. */
  const openDialog = (action: () => void) => () => { suppressFocusRef.current = true; action(); };
  return <DropdownMenu open={open} onOpenChange={setOpen}>
    <DropdownMenuTrigger asChild>
      <button ref={triggerRef} type="button" className="avatar-button relative" aria-label={t('userMenu')}>{initials ? <span data-testid="avatar-initials" aria-hidden="true" className="text-xs font-semibold leading-none">{initials}</span> : <UserIcon className="size-4" aria-hidden="true" />}{unread > 0 && <span data-testid="avatar-unread-dot" aria-hidden="true" className="absolute right-0 top-0 size-2.5 rounded-full border-2 border-background bg-primary sm:hidden" />}</button>
    </DropdownMenuTrigger>
    <DropdownMenuContent align="end" onCloseAutoFocus={(event) => {
      if (suppressFocusRef.current) {
        event.preventDefault();
        suppressFocusRef.current = false;
      }
    }}>
      <DropdownMenuLabel className="grid gap-0.5"><span>{t('localAccount')}</span><span className="text-xs font-normal text-muted-foreground">{a('ownerAccount')}</span></DropdownMenuLabel>
      <DropdownMenuSeparator />
      <div className="sm:hidden" data-testid="user-menu-mobile">
        {selection && workspaces.length > 0 && <>
          <DropdownMenuLabel>{w('switcherLabel')}</DropdownMenuLabel>
          <DropdownMenuRadioGroup value={selection.id} onValueChange={selectWorkspace}>
            {workspaces.map((ws) => <DropdownMenuRadioItem key={ws.id} value={ws.id}>{ws.name}{ws.role === 'member' ? ` · ${w('memberBadge')}` : ''}</DropdownMenuRadioItem>)}
          </DropdownMenuRadioGroup>
          <DropdownMenuSeparator />
        </>}
        <DropdownMenuLabel>{t('notificationsLabel')}</DropdownMenuLabel>
        <div className="max-h-56 overflow-y-auto"><NotificationItems /></div>
        <DropdownMenuSeparator />
      </div>
      <DropdownMenuItem onSelect={openDialog(onOpenPreferences)}>{t('userSettings')}</DropdownMenuItem>
      <DropdownMenuItem asChild><Link href="/onboarding">{t('workspaceOnboarding')}</Link></DropdownMenuItem>
      <DropdownMenuItem asChild><Link href="/knowledge/memory">{a('assistantMemory')}</Link></DropdownMenuItem>
      <DropdownMenuItem onSelect={openDialog(onOpenAccount)}>{t('accountSettings')}</DropdownMenuItem>
      <DropdownMenuItem onSelect={openDialog(onOpenChangePassword)}>{a('changePassword')}</DropdownMenuItem>
      <DropdownMenuItem disabled={signOutPending} onSelect={onSignOut}>{t('signOut')}</DropdownMenuItem>
    </DropdownMenuContent>
  </DropdownMenu>;
}
