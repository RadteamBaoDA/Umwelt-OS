'use client';

import Link from 'next/link';
import { useRef, useState, type RefObject } from 'react';
import { useTranslations } from 'next-intl';
import { UserIcon } from 'lucide-react';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';

export type UserMenuProps = {
  onOpenPreferences: () => void;
  onOpenAccount: () => void;
  onSignOut: () => void;
  signOutPending?: boolean;
  /** Lets the shell return focus to the avatar after the dialogs it opens close. */
  triggerRef?: RefObject<HTMLButtonElement | null>;
};

/** Header avatar menu; owns its open state and focus return. Wave 5 extends the items, the shell only passes handlers. */
export function UserMenu({ onOpenPreferences, onOpenAccount, onSignOut, signOutPending, triggerRef }: UserMenuProps) {
  const t = useTranslations('shell');
  const a = useTranslations('account');
  const [open, setOpen] = useState(false);
  const suppressFocusRef = useRef(false);
  /** Runs a dialog-opening action without the menu stealing focus back on close. */
  const openDialog = (action: () => void) => () => { suppressFocusRef.current = true; action(); };
  return <DropdownMenu open={open} onOpenChange={setOpen}>
    <DropdownMenuTrigger asChild>
      <button ref={triggerRef} type="button" className="avatar-button" aria-label={t('userMenu')}><UserIcon className="size-4" aria-hidden="true" /></button>
    </DropdownMenuTrigger>
    <DropdownMenuContent align="end" onCloseAutoFocus={(event) => {
      if (suppressFocusRef.current) {
        event.preventDefault();
        suppressFocusRef.current = false;
      }
    }}>
      <DropdownMenuLabel className="grid gap-0.5"><span>{t('localAccount')}</span><span className="text-xs font-normal text-muted-foreground">{a('ownerAccount')}</span></DropdownMenuLabel>
      <DropdownMenuSeparator />
      <DropdownMenuItem onSelect={openDialog(onOpenPreferences)}>{t('userSettings')}</DropdownMenuItem>
      <DropdownMenuItem asChild><Link href="/onboarding">{t('workspaceOnboarding')}</Link></DropdownMenuItem>
      <DropdownMenuItem asChild><Link href="/knowledge/memory">{a('assistantMemory')}</Link></DropdownMenuItem>
      <DropdownMenuItem onSelect={openDialog(onOpenAccount)}>{t('accountSettings')}</DropdownMenuItem>
      <DropdownMenuItem disabled={signOutPending} onSelect={onSignOut}>{t('signOut')}</DropdownMenuItem>
    </DropdownMenuContent>
  </DropdownMenu>;
}
