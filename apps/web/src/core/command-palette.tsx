'use client';

import { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { commandDestinations } from '@/core/module-registry';
import { destinationEnabled, type ModuleAvailability } from '@/core/module-registry';
import { apiRequest } from '@/core/api';
import { useQuery } from '@tanstack/react-query';
import { useGuardedNavigation } from '@/core/guarded-navigation';

/** Checks whether a key event originated in an editable control. */
function isEditing(target: EventTarget | null) {
  return target instanceof HTMLElement && Boolean(target.closest('input, textarea, select, [contenteditable], [role="textbox"]'));
}

/** Registers keyboard navigation and renders the command palette for workspace destinations. */
export function CommandPalette() {
  const { navigate } = useGuardedNavigation();
  const t = useTranslations('shell');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const moduleAvailability = useQuery({
    queryKey: ['module-lifecycle'],
    queryFn: () => apiRequest<ModuleAvailability>('/api/v1/settings/modules'),
    refetchOnWindowFocus: true,
  });
  const actions = commandDestinations.filter((action) => destinationEnabled(action, moduleAvailability.data)
    && t(action.messageKey).toLocaleLowerCase().includes(filter.toLocaleLowerCase()));

  useEffect(() => {
    /** Handles the palette shortcut only when the key event is not consumed by an editable control. */
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setOpen((value) => !value);
      } else if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !isEditing(event.target) && !dialogRef.current?.open) {
        const searchField = document.querySelector<HTMLInputElement>('[data-search-query]');
        if (searchField) { event.preventDefault(); searchField.focus(); }
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) { dialog.showModal(); inputRef.current?.focus(); }
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return <><Button type="button" className="secondary" onClick={() => setOpen(true)} aria-keyshortcuts="Control+K Meta+K">{t('commands')} <span className="muted">⌘K</span></Button>
    <dialog ref={dialogRef} className="command-dialog" aria-label={t('workspaceCommands')} onClose={() => { setOpen(false); setFilter(''); }}>
      <div className="section-heading"><h2>{t('goTo')}</h2><Button type="button" className="secondary" onClick={() => setOpen(false)}>{t('close')}</Button></div>
      <label className="label" htmlFor="command-filter">{t('filterActions')}</label><input ref={inputRef} id="command-filter" className="input" value={filter} onChange={(event) => setFilter(event.target.value)} />
      <ul className="command-actions">{actions.map((action) => <li key={action.id}><button type="button" className="text-button" onClick={() => { if (navigate(action.href)) setOpen(false); }}>{t(action.messageKey)}</button></li>)}</ul>
      {actions.length === 0 && <p className="muted">{t('noActions')}</p>}
    </dialog>
  </>;
}
