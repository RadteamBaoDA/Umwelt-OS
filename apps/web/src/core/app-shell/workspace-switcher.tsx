'use client';

import { useTranslations } from 'next-intl';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Badge } from '@/components/ui/badge';
import { useWorkspace } from '@/core/workspace-context';

/** Shows the selected workspace in the top bar and marks cross-workspace administrator access as read-only. */
export function WorkspaceSwitcher() {
  const t = useTranslations('workspaces');
  const { workspaces, selection, selectWorkspace } = useWorkspace();
  if (!selection) return null;
  return <div className="flex items-center gap-2">
    <Select value={selection.id} onValueChange={selectWorkspace}>
    <SelectTrigger aria-label={t('switcherLabel')} className="max-w-48 sm:max-w-56"><SelectValue /></SelectTrigger>
    <SelectContent>{workspaces.map((w) => <SelectItem key={w.id} value={w.id}>
      {w.name}{w.role === 'member' ? ` · ${t('memberBadge')}` : ''}
    </SelectItem>)}</SelectContent>
    </Select>
    {selection.role === 'admin_viewer' && <Badge variant="outline">{t('readOnlyBadge')}</Badge>}
  </div>;
}
