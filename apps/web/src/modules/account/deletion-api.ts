import { ApiError, apiRequestRaw, csrfHeaders } from '@/core/api';

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
  const response = await apiRequestRaw('/api/v1/auth/account/deletion-status', { signal }, { emitAuthEvents: false });
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
    // The shared raw path refreshes a rejected CSRF token once; auth events stay off because a 401 here is recovered
    // through the preissued receipt instead of ending the session.
    response = await apiRequestRaw('/api/v1/auth/account/deletion', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
      body: JSON.stringify({ current_password: currentPassword, confirmation: 'DELETE' }),
    }, { emitAuthEvents: false });
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
