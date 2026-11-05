import { assignAuthorizationUrl } from '@/core/safe-url';
import { connectorKeys } from './api';
import { saveConfiguration } from './connector-persistence';
import type { EditorContext, SourceFence } from './connector-editor-context';
import type { RequestToken } from './use-connector-requests';

/** Providers whose consent flow is Google OAuth with a resource picker. */
export const GOOGLE_PROVIDERS = ['google_mail', 'google_calendar', 'google_drive'] as const;
/** One of the Google providers. */
export type GoogleProvider = (typeof GOOGLE_PROVIDERS)[number];

/** True for the Google providers (mail, calendar, drive). */
export function isGoogleProvider(provider: string): provider is GoogleProvider {
  return (GOOGLE_PROVIDERS as readonly string[]).includes(provider);
}

/** Options of one OAuth consent start: the busy action name, the error shown on failure and the provider-specific start call. */
type OAuthStart = {
  action: string;
  failureKey: string;
  saved?: { id: string; revision: number };
  start: (fence: SourceFence, token: RequestToken) => Promise<{ authorization_url: string }>;
};

/** Source and revision an OAuth start applies to: the just-saved configuration, else the clean saved draft; null when none qualifies. */
async function resolveOAuthTarget(ctx: EditorContext, savedConfiguration: { id: string; revision: number } | undefined) {
  const { draft } = ctx;
  const saved = savedConfiguration ?? (draft.dirty || draft.revision === 0 ? await saveConfiguration(ctx) : null);
  const id = saved?.id ?? draft.sourceId;
  const expectedRevision = saved?.revision ?? draft.revisionRef.current;
  if (!id || draft.dirtyRef.current || expectedRevision < 1) return null;
  return { id, revision: expectedRevision };
}

/** True while the editor still shows exactly the source, generation and revision of the fence, with a clean draft and a live request. */
function consentIsCurrent(ctx: EditorContext, token: RequestToken, fence: SourceFence): boolean {
  const { draft } = ctx;
  return (
    ctx.requests.requestIsCurrent(token) &&
    draft.sourceIdRef.current === fence.id &&
    draft.sourceGenerationRef.current === fence.generation &&
    draft.revisionRef.current === fence.revision &&
    !draft.dirtyRef.current
  );
}

/**
 * Saves the draft when it has unsaved scope, then starts the source-revision-bound read-only OAuth flow and follows its
 * authorization redirect. The redirect happens only while the editor still shows exactly the same source, generation and
 * revision with a clean draft; any change in between drops the result.
 */
export async function startOAuthConnection(ctx: EditorContext, { action, failureKey, saved, start }: OAuthStart) {
  const { draft, requests } = ctx;
  const target = await resolveOAuthTarget(ctx, saved);
  if (!target) return;
  const token = requests.beginRequest(action);
  if (!token) return;
  const fence = { id: target.id, generation: draft.sourceGenerationRef.current, revision: target.revision };
  draft.setError('');
  try {
    const result = await start(fence, token);
    if (consentIsCurrent(ctx, token, fence)) assignAuthorizationUrl(result.authorization_url);
  } catch {
    if (requests.requestIsCurrent(token)) draft.setError(failureKey);
  } finally {
    requests.endRequest(token);
  }
}

/** Options of one grant revocation: the busy action, the status the button was rendered from and the provider calls. */
type OAuthRevoke = {
  action: string;
  failureKey: string;
  status: { state: string; token_revision: number } | undefined;
  revoke: (fence: Omit<SourceFence, 'id'>, tokenRevision: number, token: RequestToken) => Promise<unknown>;
  refetch: () => Promise<unknown>;
};

/** Revokes only the current grant revision of a clean, saved source and refreshes its secret-free status and configuration. */
export async function revokeOAuthConnection(ctx: EditorContext, { action, failureKey, status, revoke, refetch }: OAuthRevoke) {
  const { draft, requests, queryClient } = ctx;
  const id = draft.sourceId;
  if (!id || !status || status.state !== 'ready' || draft.dirtyRef.current) return;
  const token = requests.beginRequest(action);
  if (!token) return;
  try {
    await revoke({ generation: draft.sourceGenerationRef.current, revision: draft.revisionRef.current }, status.token_revision, token);
    if (requests.requestIsCurrent(token)) {
      await Promise.all([refetch(), queryClient.invalidateQueries({ queryKey: connectorKeys.configuration(id) })]);
    }
  } catch {
    if (requests.requestIsCurrent(token)) draft.setError(failureKey);
  } finally {
    requests.endRequest(token);
  }
}
