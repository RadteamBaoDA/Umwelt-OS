'use client';

import { useQuery } from '@tanstack/react-query';
import { disconnectRedditOAuth, getRedditOAuthStatus, startRedditOAuth } from './api';
import { revokeOAuthConnection, startOAuthConnection } from './connector-oauth';
import type { EditorContext } from './connector-editor-context';

/** Status poll interval of an OAuth grant. */
const STATUS_REFETCH_MS = 10_000;

/**
 * Reddit read-only OAuth connection of the edited source: the polled grant status plus connect and disconnect actions.
 * Connect saves unsaved scope first and only follows the authorization redirect for the exact source revision it was started for.
 */
export function useRedditConnection(ctx: EditorContext) {
  const { provider, sourceId } = ctx.draft;
  const active = Boolean(sourceId) && provider === 'reddit';
  const redditStatusQuery = useQuery({
    queryKey: ['reddit-oauth-status', sourceId],
    queryFn: ({ signal }) => getRedditOAuthStatus(sourceId, signal),
    enabled: active,
    refetchInterval: active ? STATUS_REFETCH_MS : false,
  });

  /** Saves the selected subreddit, then starts the source-bound read-only Reddit OAuth flow. */
  async function connectReddit(saved?: { id: string; revision: number }) {
    if (provider !== 'reddit') return;
    await startOAuthConnection(ctx, {
      action: 'reddit-connect',
      failureKey: 'redditConnectionFailed',
      saved,
      start: (fence, token) => startRedditOAuth(fence.id, fence.generation, fence.revision, ctx.csrfToken, token.controller.signal),
    });
  }

  /** Revokes only the current Reddit grant revision and refreshes its secret-free status. */
  async function disconnectReddit() {
    await revokeOAuthConnection(ctx, {
      action: 'reddit-disconnect',
      failureKey: 'redditDisconnectFailed',
      status: redditStatusQuery.data,
      refetch: redditStatusQuery.refetch,
      revoke: (fence, tokenRevision, token) =>
        disconnectRedditOAuth(sourceId, fence.generation, fence.revision, tokenRevision, ctx.csrfToken, token.controller.signal),
    });
  }

  return { redditStatusQuery, connectReddit, disconnectReddit };
}
