'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bell } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { apiRequest } from '@/core/api';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listNotifications, type AppNotification, notificationKeys, setNotificationRead } from './api';

/** Accepts only relative in-app paths so a stored link can never navigate off-site. */
function safeLink(link: string | null): string {
  return link && link.startsWith('/') && !link.startsWith('//') && !link.includes(String.fromCharCode(92)) ? link : '/app';
}

/**
 * Notification bell with an unread count. Opening an item's link marks it read; unread state is
 * conveyed by text ("Unread"), not color alone.
 */
export function NotificationBell() {
  const t = useTranslations('notifications');
  const display = useDisplayPreferences();
  const client = useQueryClient();
  const session = useQuery({ queryKey: ['session'], queryFn: () => apiRequest<{ authenticated: true; csrfToken: string }>('/api/v1/auth/session') });
  const page = useQuery({ queryKey: notificationKeys.all, queryFn: ({ signal }) => listNotifications(signal), refetchInterval: 60_000 });
  const markRead = useMutation({
    mutationFn: (id: string) => setNotificationRead(id, true, session.data!.csrfToken),
    onSettled: () => client.invalidateQueries({ queryKey: notificationKeys.all }),
  });
  const unread = page.data?.unread_count ?? 0;
  /** Localized title for a known kind, else the stored legacy title. */
  const titleOf = (item: AppNotification) => {
    const key = `kind_${item.kind.replace(/\W/g, '_')}`;
    return t.has(key) ? t(key, item.params) : item.title ?? item.kind;
  };
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="outline" size="icon" aria-label={t('open', { count: unread })}>
          <Bell className="h-4 w-4" aria-hidden />
          {unread > 0 && <span className="text-xs font-bold" aria-hidden>{unread > 99 ? '99+' : unread}</span>}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80 max-w-[calc(100vw-2rem)]">
        {page.isError && <p role="alert" className="p-2 text-xs text-destructive">{t('loadError')}</p>}
        {page.data && page.data.items.length === 0 && <p role="status" className="p-2 text-xs text-muted-foreground">{t('empty')}</p>}
        {page.data?.items.map((item) => (
          <DropdownMenuItem
            key={item.id}
            asChild
            onSelect={() => { if (!item.read_at && session.data) markRead.mutate(item.id); }}
          >
            <Link href={safeLink(item.link)} className="flex flex-col items-start gap-0.5">
              <span className="text-xs font-semibold">{!item.read_at && <span className="mr-1 rounded bg-primary px-1 text-[10px] text-primary-foreground">{t('unread')}</span>}{titleOf(item)}</span>
              {!t.has(`kind_${item.kind.replace(/\W/g, '_')}`) && item.body && <span className="text-xs text-muted-foreground">{item.body}</span>}
              <span className="text-[10px] text-muted-foreground">{formatDateTime(item.created_at, display.locale, display.timezone)}</span>
            </Link>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
