'use client';

import { useQuery } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { ApiError } from '@/core/api';
import { assignAuthorizationUrl } from '@/core/safe-url';
import {
  acknowledgeGitHubReconnect,
  disconnectGitHubOAuth,
  getGitHubPeers,
  getGitHubStatus,
  refreshGitHubOAuth,
  resetGitHubSync,
  startGitHubOAuth,
  type GitHubPeer,
  type GitHubStatus,
} from './api';
import type { EditorContext } from './connector-editor-context';
import type { RequestToken } from './use-connector-requests';

/** Status poll interval of the GitHub grant. */
const STATUS_REFETCH_MS = 10_000;

/** Operation and source revision captured when the owner opened the recovery review; confirmation re-checks all of it. */
export type GitHubRecoveryReview = {
  operationId: string;
  kind: 'authorization' | 'refresh';
  originSourceId: string;
  originGeneration: number;
  originRevision: number;
  sourceId: string;
  generation: number;
  revision: number;
};
/** Peer inventory and source revisions the owner reviewed before revoking the app-wide grant. */
type PeerReview = { sourceId: string; sourceGeneration: number; revision: number; peers: GitHubPeer[] };

/** Review and dialog state of GitHub recovery and disconnect (everything the confirmation dialogs render or re-check). */
function useGitHubReviewState() {
  const [githubPeers, setGithubPeers] = useState<GitHubPeer[]>([]);
  const [githubDisconnectOpen, setGithubDisconnectOpen] = useState(false);
  const [githubRecoveryReview, setGithubRecoveryReview] = useState<GitHubRecoveryReview | null>(null);
  const [githubRecoveryOpen, setGithubRecoveryOpen] = useState(false);
  const githubReviewRef = useRef<PeerReview | null>(null);
  return {
    githubPeers,
    setGithubPeers,
    githubDisconnectOpen,
    setGithubDisconnectOpen,
    githubRecoveryReview,
    setGithubRecoveryReview,
    githubRecoveryOpen,
    setGithubRecoveryOpen,
    githubReviewRef,
  };
}
type ReviewState = ReturnType<typeof useGitHubReviewState>;

/** Checks that an async GitHub action still owns the exact saved editor source, generation, and configuration revision. */
function githubRequestIsCurrent(
  ctx: EditorContext,
  token: RequestToken,
  id: string,
  generation: number,
  expectedRevision: number,
): boolean {
  const { draft } = ctx;
  return (
    ctx.requests.requestIsCurrent(token) &&
    token.sourceId === id &&
    draft.sourceIdRef.current === id &&
    draft.sourceGenerationRef.current === generation &&
    draft.revisionRef.current === expectedRevision &&
    !draft.dirtyRef.current
  );
}

/** True when the status no longer describes the unresolved operation the owner reviewed, or the selected source moved. */
function recoveryIsStale(ctx: EditorContext, reviewed: GitHubRecoveryReview, status: GitHubStatus | undefined): boolean {
  const { draft } = ctx;
  const statusMoved =
    !status ||
    status.operation_id !== reviewed.operationId ||
    status.operation_source_id !== reviewed.originSourceId ||
    status.operation_source_generation !== reviewed.originGeneration ||
    status.operation_configuration_revision !== reviewed.originRevision;
  const sourceMoved =
    draft.sourceIdRef.current !== reviewed.sourceId ||
    draft.sourceGenerationRef.current !== reviewed.generation ||
    draft.revisionRef.current !== reviewed.revision ||
    draft.dirtyRef.current;
  return statusMoved || sourceMoved || ctx.activation.sourceStatus !== 'active';
}

/** Operation id to settle while revoking: only an unresolved provider-revoke coordination carries one. */
function pendingRevokeOperationId(status: GitHubStatus | undefined): string | undefined {
  const unresolved =
    Boolean(status?.recovery_available) &&
    ['reconciliation_required', 'revoking'].includes(status?.coordinator_state ?? '') &&
    ['provider_revoke_outcome_unknown', 'provider_revoke_pending'].includes(status?.coordinator_error_code ?? '');
  return unresolved ? (status?.operation_id ?? undefined) : undefined;
}

/** Drops a reviewed peer inventory and closes its dialog (used when the reviewed state is no longer valid). */
function dropPeerReview(review: ReviewState) {
  review.githubReviewRef.current = null;
  review.setGithubPeers([]);
  review.setGithubDisconnectOpen(false);
}

/** Review state plus the polled status: everything the GitHub actions read besides the editor context. */
type GitHubBundle = { review: ReviewState; status: ReturnType<typeof useGitHubStatus> };

/** Polls the GitHub grant status while the edited source is a GitHub source. */
function useGitHubStatus(sourceId: string, provider: string) {
  const active = Boolean(sourceId) && provider === 'github';
  return useQuery({
    queryKey: ['github-oauth-status', sourceId],
    queryFn: ({ signal }) => getGitHubStatus(sourceId, signal),
    enabled: active,
    refetchInterval: active ? STATUS_REFETCH_MS : false,
  });
}

/** Starts OAuth only after the owner has separately resolved any pending operation, then follows its browser-bound redirect. */
async function connectGitHub(ctx: EditorContext) {
  const { draft, requests } = ctx;
  const { sourceId } = draft;
  if (!sourceId || draft.provider !== 'github' || draft.dirtyRef.current) return;
  const token = requests.beginRequest('github-connect');
  if (!token) return;
  const generation = draft.sourceGenerationRef.current;
  const expectedRevision = draft.revisionRef.current;
  draft.setError('');
  try {
    const result = await startGitHubOAuth(sourceId, generation, expectedRevision, ctx.csrfToken, token.controller.signal);
    if (!githubRequestIsCurrent(ctx, token, sourceId, generation, expectedRevision)) return;
    assignAuthorizationUrl(result.authorization_url);
  } catch {
    if (requests.requestIsCurrent(token)) draft.setError('githubConnectionFailed');
  } finally {
    requests.endRequest(token);
  }
}

/** Restarts GitHub history only against the current saved configuration and reviewed scope digest. */
async function resetGitHubHistory(ctx: EditorContext, gh: GitHubBundle) {
  const { draft, requests } = ctx;
  const { sourceId } = draft;
  const status = gh.status.data;
  if (!sourceId || draft.provider !== 'github' || draft.dirtyRef.current || !status?.sync.scope_sha256) return;
  const token = requests.beginRequest('github-reset');
  if (!token) return;
  const generation = draft.sourceGenerationRef.current;
  const expectedRevision = draft.revisionRef.current;
  try {
    await resetGitHubSync(sourceId, generation, expectedRevision, status.sync.scope_sha256, ctx.csrfToken, token.controller.signal);
    if (!githubRequestIsCurrent(ctx, token, sourceId, generation, expectedRevision)) return;
    await gh.status.refetch();
  } catch {
    if (requests.requestIsCurrent(token)) draft.setError('githubConnectionFailed');
  } finally {
    requests.endRequest(token);
  }
}

/** Captures the owner-wide operation origin and selected clean source for a separate explicit recovery confirmation. */
function reviewGitHubRecovery(ctx: EditorContext, gh: GitHubBundle) {
  const { draft } = ctx;
  const status = gh.status.data;
  if (
    !status?.recovery_available ||
    !status.operation_id ||
    !status.operation_source_id ||
    !['authorization', 'refresh'].includes(status.operation_kind ?? '') ||
    !draft.sourceId ||
    ctx.activation.sourceStatus !== 'active' ||
    draft.dirtyRef.current ||
    draft.revisionRef.current < 1
  )
    return;
  gh.review.setGithubRecoveryReview({
    operationId: status.operation_id,
    kind: status.operation_kind as 'authorization' | 'refresh',
    originSourceId: status.operation_source_id,
    originGeneration: status.operation_source_generation ?? 0,
    originRevision: status.operation_configuration_revision ?? 0,
    sourceId: draft.sourceId,
    generation: draft.sourceGenerationRef.current,
    revision: draft.revisionRef.current,
  });
  gh.review.setGithubRecoveryOpen(true);
}

/** Acknowledges only the captured unresolved operation, then starts consent for the still-current selected source. */
async function confirmGitHubRecovery(ctx: EditorContext, gh: GitHubBundle) {
  const { review, status } = gh;
  const reviewed = review.githubRecoveryReview;
  if (!reviewed || recoveryIsStale(ctx, reviewed, status.data)) {
    review.setGithubRecoveryOpen(false);
    review.setGithubRecoveryReview(null);
    ctx.draft.setError('conflict');
    return;
  }
  const { requests, draft } = ctx;
  const token = requests.beginRequest('github-recovery');
  if (!token) return;
  draft.setError('');
  const stillCurrent = () => githubRequestIsCurrent(ctx, token, reviewed.sourceId, reviewed.generation, reviewed.revision);
  try {
    await acknowledgeGitHubReconnect(reviewed.sourceId, reviewed.operationId, ctx.csrfToken, token.controller.signal);
    if (!stillCurrent()) return;
    await status.refetch();
    const result = await startGitHubOAuth(
      reviewed.sourceId,
      reviewed.generation,
      reviewed.revision,
      ctx.csrfToken,
      token.controller.signal,
    );
    if (!stillCurrent()) return;
    review.setGithubRecoveryOpen(false);
    review.setGithubRecoveryReview(null);
    assignAuthorizationUrl(result.authorization_url);
  } catch {
    if (requests.requestIsCurrent(token)) draft.setError('githubConnectionFailed');
    void status.refetch();
  } finally {
    requests.endRequest(token);
  }
}

/** Rotates the stored GitHub grant and reloads its secret-free status. */
async function refreshGitHub(ctx: EditorContext, gh: GitHubBundle) {
  const { draft, requests } = ctx;
  const { sourceId } = draft;
  if (!sourceId) return;
  const token = requests.beginRequest('github-refresh');
  if (!token) return;
  const generation = draft.sourceGenerationRef.current;
  const expectedRevision = draft.revisionRef.current;
  draft.setError('');
  try {
    await refreshGitHubOAuth(sourceId, ctx.csrfToken, token.controller.signal);
    if (!githubRequestIsCurrent(ctx, token, sourceId, generation, expectedRevision)) return;
    await gh.status.refetch();
  } catch {
    if (requests.requestIsCurrent(token)) draft.setError('githubConnectionFailed');
    void gh.status.refetch();
  } finally {
    requests.endRequest(token);
  }
}

/** Loads a bounded peer inventory before showing the app-wide revoke confirmation. */
async function reviewGitHubDisconnect(ctx: EditorContext, gh: GitHubBundle) {
  const { draft, requests } = ctx;
  const { sourceId } = draft;
  if (!sourceId) return;
  const token = requests.beginRequest('github-peers');
  if (!token) return;
  const generation = draft.sourceGenerationRef.current;
  const expectedRevision = draft.revisionRef.current;
  draft.setError('');
  try {
    const inventory = await getGitHubPeers(sourceId, token.controller.signal);
    if (!githubRequestIsCurrent(ctx, token, sourceId, generation, expectedRevision)) return;
    if (!inventory.complete || inventory.peers.length === 0) throw new Error('github_peer_inventory_incomplete');
    gh.review.setGithubPeers(inventory.peers);
    gh.review.githubReviewRef.current = { sourceId, sourceGeneration: generation, revision: expectedRevision, peers: inventory.peers };
    gh.review.setGithubDisconnectOpen(true);
  } catch {
    if (requests.requestIsCurrent(token)) draft.setError('githubInventoryFailed');
  } finally {
    requests.endRequest(token);
  }
}

/** Records a failed revoke: a 409 invalidates the reviewed inventory, anything else is a connection failure. */
function publishDisconnectFailure(ctx: EditorContext, gh: GitHubBundle, cause: unknown) {
  if (cause instanceof ApiError && cause.status === 409) {
    dropPeerReview(gh.review);
    ctx.draft.setError('conflict');
  } else ctx.draft.setError('githubConnectionFailed');
}

/** True when the editor no longer shows the exact source revision the peer inventory was reviewed against. */
function peerReviewIsStale(ctx: EditorContext, reviewed: PeerReview): boolean {
  const { draft } = ctx;
  return (
    draft.sourceIdRef.current !== reviewed.sourceId ||
    draft.sourceGenerationRef.current !== reviewed.sourceGeneration ||
    draft.revisionRef.current !== reviewed.revision ||
    draft.dirtyRef.current
  );
}

/** Submits the exact peer revisions the owner reviewed and refreshes local status after revocation. */
async function confirmGitHubDisconnect(ctx: EditorContext, gh: GitHubBundle) {
  const { draft, requests } = ctx;
  const { review, status } = gh;
  const reviewed = review.githubReviewRef.current;
  if (!reviewed || reviewed.peers.length === 0) return;
  const token = requests.beginRequest('github-disconnect');
  if (!token) return;
  const operationId = pendingRevokeOperationId(status.data);
  if (peerReviewIsStale(ctx, reviewed)) {
    dropPeerReview(review);
    draft.setError('conflict');
    requests.endRequest(token);
    return;
  }
  draft.setError('');
  try {
    await disconnectGitHubOAuth(reviewed.sourceId, reviewed.peers, ctx.csrfToken, operationId, token.controller.signal);
    if (!githubRequestIsCurrent(ctx, token, reviewed.sourceId, reviewed.sourceGeneration, reviewed.revision)) return;
    review.setGithubDisconnectOpen(false);
    review.githubReviewRef.current = null;
    await status.refetch();
    await ctx.onChanged();
  } catch (cause) {
    if (requests.requestIsCurrent(token)) publishDisconnectFailure(ctx, gh, cause);
    void status.refetch();
  } finally {
    requests.endRequest(token);
  }
}

/**
 * GitHub App connection of the edited source: the polled grant status plus connect, history reset, recovery review/confirm,
 * token refresh and app-wide disconnect with a reviewed peer inventory. Every completion is applied only while the editor
 * still shows exactly the source, generation and revision the action started against, with a clean draft.
 */
export function useGitHubConnection(ctx: EditorContext) {
  const review = useGitHubReviewState();
  const githubStatusQuery = useGitHubStatus(ctx.draft.sourceId, ctx.draft.provider);
  const gh: GitHubBundle = { review, status: githubStatusQuery };
  return {
    githubPeers: review.githubPeers,
    githubDisconnectOpen: review.githubDisconnectOpen,
    setGithubDisconnectOpen: review.setGithubDisconnectOpen,
    githubRecoveryReview: review.githubRecoveryReview,
    setGithubRecoveryReview: review.setGithubRecoveryReview,
    githubRecoveryOpen: review.githubRecoveryOpen,
    setGithubRecoveryOpen: review.setGithubRecoveryOpen,
    githubStatusQuery,
    connectGitHub: () => connectGitHub(ctx),
    resetGitHubHistory: () => resetGitHubHistory(ctx, gh),
    reviewGitHubRecovery: () => reviewGitHubRecovery(ctx, gh),
    confirmGitHubRecovery: () => confirmGitHubRecovery(ctx, gh),
    refreshGitHub: () => refreshGitHub(ctx, gh),
    reviewGitHubDisconnect: () => reviewGitHubDisconnect(ctx, gh),
    confirmGitHubDisconnect: () => confirmGitHubDisconnect(ctx, gh),
  };
}
