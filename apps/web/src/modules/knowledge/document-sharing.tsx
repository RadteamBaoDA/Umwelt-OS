'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Share2 } from 'lucide-react';
import { useTranslations } from 'next-intl';
import type { ReactNode } from 'react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useWorkspace } from '@/core/workspace-context';
import { grantShare, listShares, listWorkspaceMembers, revokeShare, shareKeys, type ShareResourceType, type ShareRow, type WorkspaceMember } from './api';

/** Maps a share failure to a message key: 403 role, 404 invisible, 409 refresh. */
export function shareErrorKey(error: unknown): 'errorRole' | 'errorInvisible' | 'errorRefresh' | 'errorFailed' {
  if (!(error instanceof ApiError)) return 'errorFailed';
  if (error.status === 403) return 'errorRole';
  if (error.status === 404) return 'errorInvisible';
  if (error.status === 409) return 'errorRefresh';
  return 'errorFailed';
}

type ShareDialogProps = {
  resourceType: ShareResourceType;
  resourceId: string;
  /** Document current_version or brief revision the grant binds to. */
  resourceRevision: number;
  semantics: string;
  /** Replaces the default grant call (the brief flow decorates evidence conflicts). */
  grant?: (member: WorkspaceMember, resourceRevision: number, csrfToken: string) => Promise<ShareRow>;
  /** Extra content for a grant failure, such as the evidence documents still to share. */
  renderError?: (error: unknown) => ReactNode;
};

/**
 * Owner-only dialog listing workspace members with an explicit Share / Revoke choice per member.
 * It never offers a whole-workspace share. Renders nothing for members.
 */
export function ShareDialog({ resourceType, resourceId, resourceRevision, semantics, grant, renderError }: ShareDialogProps) {
  const t = useTranslations('sharing');
  const { selection, isOwner } = useWorkspace();
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const workspaceId = selection?.id ?? '';
  const enabled = isOwner && Boolean(workspaceId);
  const members = useQuery({ queryKey: shareKeys.members(workspaceId), queryFn: () => listWorkspaceMembers(workspaceId), enabled });
  const shares = useQuery({ queryKey: shareKeys.list(workspaceId, resourceType, resourceId), queryFn: () => listShares(workspaceId, resourceType, resourceId), enabled });
  const refresh = () => Promise.all([
    client.invalidateQueries({ queryKey: shareKeys.members(workspaceId) }),
    client.invalidateQueries({ queryKey: shareKeys.list(workspaceId, resourceType, resourceId) }),
  ]);
  const grantMutation = useMutation({
    mutationFn: (member: WorkspaceMember) => (grant ?? ((m, rev, token) => grantShare(workspaceId, resourceType, resourceId, m, rev, token)))(member, resourceRevision, csrfToken),
    onSettled: refresh,
  });
  const revokeMutation = useMutation({
    mutationFn: (member: WorkspaceMember) => revokeShare(workspaceId, resourceType, resourceId, member, csrfToken),
    onSettled: refresh,
  });
  if (!isOwner) return null;

  const active = new Map((shares.data?.items ?? []).filter((row) => row.revoked_at === null).map((row) => [row.member_user_id, row]));
  const candidates = (members.data?.items ?? []).filter((member) => member.role === 'member');
  const error = grantMutation.error ?? revokeMutation.error;
  const busy = grantMutation.isPending || revokeMutation.isPending;

  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" size="sm"><Share2 className="h-3.5 w-3.5" aria-hidden />{t('share')}</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t(resourceType === 'document' ? 'titleDocument' : 'titleBrief')}</DialogTitle>
          <DialogDescription>{semantics}</DialogDescription>
        </DialogHeader>
        {(members.isPending || shares.isPending) && <p role="status" className="text-sm text-muted-foreground">{t('loading')}</p>}
        {(members.isError || shares.isError) && <p role="alert" className="text-sm text-destructive">{t(shareErrorKey(members.error ?? shares.error))}</p>}
        {members.isSuccess && candidates.length === 0 && <p className="text-sm text-muted-foreground">{t('noMembers')}</p>}
        {candidates.length > 0 && (
          <ul className="divide-y divide-border" aria-label={t('members')}>
            {candidates.map((member) => {
              const shared = active.has(member.user_id);
              const label = member.email ?? t('memberFallback', { id: member.user_id });
              return (
                <li key={member.user_id} className="flex items-center justify-between gap-2 py-2 text-sm">
                  <span className="min-w-0 truncate">{label}{shared && <span className="ml-2 text-xs text-muted-foreground">{t('shared')}</span>}</span>
                  {shared
                    ? <Button type="button" size="sm" variant="outline" disabled={busy} aria-label={t('revokeFor', { member: label })} onClick={() => revokeMutation.mutate(member)}>{t('revoke')}</Button>
                    : <Button type="button" size="sm" disabled={busy} aria-label={t('shareWith', { member: label })} onClick={() => grantMutation.mutate(member)}>{t('shareAction')}</Button>}
                </li>
              );
            })}
          </ul>
        )}
        {error && <p role="alert" className="text-sm text-destructive">{t(shareErrorKey(error))}</p>}
        {grantMutation.error && renderError?.(grantMutation.error)}
      </DialogContent>
    </Dialog>
  );
}

/** Share control for a document; binds the grant to its current version and includes later versions while active. */
export function DocumentShareButton({ documentId, currentVersion }: { documentId: string; currentVersion: number }) {
  const t = useTranslations('sharing');
  return <ShareDialog resourceType="document" resourceId={documentId} resourceRevision={currentVersion} semantics={t('semanticsDocument')} />;
}
