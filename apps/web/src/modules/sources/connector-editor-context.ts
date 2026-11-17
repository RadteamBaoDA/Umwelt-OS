import type { QueryClient } from '@tanstack/react-query';
import type { useTranslations } from 'next-intl';
import type { ConnectorCatalogEntry, ConnectorConfig, ConnectorSettings, Source } from './api';
import { errorText, scopeIssues, sourceErrorDetail } from './connector-editor-model';
import type { ConnectorActivationState } from './use-connector-activation';
import type { ConnectorDraft } from './use-connector-draft';
import type { ConnectorRequests } from './use-connector-requests';
import type { ConnectorServerSync } from './use-connector-sync';
import { ApiError } from '@/core/api';
import type { useDisplayPreferences } from '@/core/query-provider';

/** Translator of the `sources` message catalog. */
export type SourcesTranslator = ReturnType<typeof useTranslations<'sources'>>;

/**
 * Everything the connector editor's async actions read: the render's draft, request fence, activation and server-sync bundles,
 * the resolved catalog entry and effective configuration/settings, and the host callbacks. Rebuilt every render so an action
 * sees the same values the former single component body closed over.
 */
export type EditorContext = {
  t: SourcesTranslator;
  csrfToken: string;
  display: ReturnType<typeof useDisplayPreferences>;
  queryClient: QueryClient;
  source?: Source | null;
  onChanged: () => void;
  onClose: () => void;
  draft: ConnectorDraft;
  requests: ConnectorRequests;
  activation: ConnectorActivationState;
  sync: ConnectorServerSync;
  currentEntry: ConnectorCatalogEntry | undefined;
  configuration: ConnectorConfig;
  settings: ConnectorSettings;
};

/** Stable failure code of a rejected save/enable request: the API error code, `http_<status>`, or `request_failed`. */
export function failureCodeOf(cause: unknown): string {
  return cause instanceof ApiError ? (cause.code ?? `http_${cause.status}`) : 'request_failed';
}

/** Publishes a failed action: the message key, the bounded server diagnostic and the per-field issues (no submitted values). */
export function publishFailure(ctx: EditorContext, cause: unknown) {
  ctx.draft.setError(errorText(cause));
  ctx.draft.setErrorDetail(sourceErrorDetail(cause));
  ctx.draft.setIssues(scopeIssues(cause));
}

/** Clears the previous outcome (error, detail, issues, notice) before a new save or validation starts. */
export function clearOutcome(ctx: EditorContext) {
  ctx.draft.setError('');
  ctx.draft.setErrorDetail('');
  ctx.draft.setIssues([]);
  ctx.draft.setNotice('');
}

/** Revisions of an OAuth-bound action; a completion is applied only while the editor still shows exactly this source revision. */
export type SourceFence = { id: string; generation: number; revision: number };
