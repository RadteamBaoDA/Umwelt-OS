import { ApiError, apiRequest, csrfHeaders } from '@/core/api';

export type DeletionPreview = {
  can_delete: boolean;
  blockers: ('workspace_has_members' | 'workspace_has_live_invitations')[];
  workspace_count: number;
  instance_operator: boolean;
};

export type DeletionStatus = {
  status: 'queued' | 'running' | 'failed' | 'succeeded';
  phase: string;
  error_code: string | null;
};

/** Read receipt-only progress without emitting global authentication events after logout. */
export async function readAccountDeletionStatus(signal?: AbortSignal): Promise<DeletionStatus> {
  const response = await fetch('/api/v1/auth/account/deletion-status', { cache: 'no-store', credentials: 'same-origin', signal });
  if (!response.ok) throw new ApiError(response.status, 'Deletion status unavailable');
  return response.json() as Promise<DeletionStatus>;
}

/** Submit password confirmation and recover ambiguous acceptance through the preissued HttpOnly cookie.
 * Credentials stay in the request body and are never cached or stored. Receipt lookup can only
 * observe a previously accepted operation; it cannot create a deletion.
 */
export async function acceptAccountDeletion(currentPassword: string, csrfToken: string): Promise<DeletionStatus> {
  let response: Response;
  try {
    const request: RequestInit = {
      method: 'POST', cache: 'no-store', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
      body: JSON.stringify({ current_password: currentPassword, confirmation: 'DELETE' }),
    };
    response = await fetch('/api/v1/auth/account/deletion', request);
    if (response.status === 403 && response.headers.get('X-CSRF-Error') === 'invalid') {
      const refreshed = await apiRequest<{ csrfToken: string }>('/api/v1/auth/session');
      window.dispatchEvent(new CustomEvent('bbd:session-refreshed', { detail: refreshed }));
      response = await fetch('/api/v1/auth/account/deletion', { ...request, headers: { 'Content-Type': 'application/json', ...csrfHeaders(refreshed.csrfToken) } });
    }
  } catch (error) {
    try { return await readAccountDeletionStatus(); } catch { throw error; }
  }
  if (response.ok) {
    try { return await response.json() as DeletionStatus; }
    catch (error) {
      try { return await readAccountDeletionStatus(); } catch { throw error; }
    }
  }
  if (response.status >= 500 || response.status === 401) {
    try { return await readAccountDeletionStatus(); } catch { /* No accepted receipt: preserve the original rejection. */ }
  }
  let detail: { code?: string; message?: string } = {};
  try { const body = await response.json(); detail = body.error ?? (typeof body.detail === 'object' ? body.detail : {}); } catch { /* Proxy failures need no body to be reported. */ }
  throw new ApiError(response.status, detail.message ?? 'Account deletion failed', detail);
}
