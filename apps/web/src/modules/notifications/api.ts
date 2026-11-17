import { apiRequest, csrfHeaders } from '@/core/api';

/** Owner notification as returned by the notifications API. */
export type AppNotification = {
  id: string;
  kind: string;
  /** Legacy display fallback; new rows carry only `kind` + `params`. */
  title: string | null;
  body: string | null;
  params: Record<string, string | number>;
  link: string | null;
  read_at: string | null;
  created_at: string;
};

/** Newest-first notifications plus the owner's total unread count. */
export type NotificationPage = { items: AppNotification[]; unread_count: number };

/** Query key shared by the bell and any later notification surface. */
export const notificationKeys = { all: ['notifications'] as const };

/** Loads the newest notifications with the unread count. */
export function listNotifications(signal?: AbortSignal): Promise<NotificationPage> {
  return apiRequest<NotificationPage>('/api/v1/notifications?limit=20', { signal });
}

/** Marks one notification read or unread under CSRF protection. */
export function setNotificationRead(id: string, read: boolean, csrfToken: string): Promise<AppNotification> {
  return apiRequest<AppNotification>(`/api/v1/notifications/${id}`, {
    method: 'PATCH',
    headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
    body: JSON.stringify({ read }),
  });
}
