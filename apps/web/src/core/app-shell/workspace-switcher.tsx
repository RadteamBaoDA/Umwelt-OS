'use client';

import { useTranslations } from 'next-intl';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useWorkspace } from '@/core/workspace-context';

/** Workspace selector for the top bar (desktop and phone); hidden while the account has a single workspace. */
export function WorkspaceSwitcher() {
  const t = useTranslations('workspaces');
  const { workspaces, selection, selectWorkspace } = useWorkspace();
  if (workspaces.length < 2 || !selection) return null;
  return <Select value={selection.id} onValueChange={selectWorkspace}>
    <SelectTrigger aria-label={t('switcherLabel')} className="max-w-40 sm:max-w-56"><SelectValue /></SelectTrigger>
    <SelectContent>{workspaces.map((w) => <SelectItem key={w.id} value={w.id}>
      {w.name}{w.role === 'member' ? ` · ${t('memberBadge')}` : ''}
    </SelectItem>)}</SelectContent>
  </Select>;
}
