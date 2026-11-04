'use client';

import { useTranslations } from 'next-intl';
import { SettingsSection } from '@/components/settings-layout';
import { Button } from '@/components/ui/button';
import { formatDateTime as formatDate } from '@/core/format';
import type { GitHubStatus } from './api';
import type { ConnectorEditorModel } from './connector-editor-types';
import { GitHubSummary } from './github-summary';

type StatusData = GitHubStatus | undefined;

/**
 * Which control set the GitHub connection panel offers, in the original precedence order: an unknown/pending revoke retry,
 * an authorization/refresh recovery review, waiting while the coordinator is busy, refresh/disconnect when ready, else connect.
 */
export function githubControlMode(data: StatusData): 'retryRevoke' | 'reviewRecovery' | 'wait' | 'ready' | 'connect' {
  if (
    data?.recovery_available &&
    ['provider_revoke_outcome_unknown', 'provider_revoke_pending'].includes(data.coordinator_error_code ?? '')
  )
    return 'retryRevoke';
  if (data?.recovery_available && ['authorization', 'refresh'].includes(data.operation_kind ?? '')) return 'reviewRecovery';
  if (data?.coordinator_state !== 'idle' && data?.coordinator_state !== undefined) return 'wait';
  if (data?.state === 'ready') return 'ready';
  return 'connect';
}

/** Expiry and scan-window lines of the GitHub status. */
function GitHubWindowNotes({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { display, githubStatusQuery } = m;
  const data = githubStatusQuery.data;
  return (
    <>
      {data?.expires_at && (
        <p>
          {t('githubExpires')}: {formatDate(data.expires_at, display.locale, display.timezone)}
        </p>
      )}
      {data && <p>{t('githubScanWindow', { days: data.sync.history_days })}</p>}
    </>
  );
}

/** Warning lines: unverified evidence, paused source and exhausted reconciliation. */
function GitHubWarningNotes({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { githubStatusQuery, source } = m;
  const sync = githubStatusQuery.data?.sync;
  return (
    <>
      {(sync?.unverified_hints ?? 0) > 0 && (
        <p className="error">{t('githubEvidenceUnverified', { count: sync?.unverified_hints ?? 0 })}</p>
      )}
      {source?.provider === 'github' && source.status === 'paused' && <p className="error">{t('githubSourcePaused')}</p>}
      {(sync?.reconcile_exhausted ?? 0) > 0 && (
        <p className="error">{t('githubReconcileExhausted', { count: sync?.reconcile_exhausted ?? 0 })}</p>
      )}
    </>
  );
}

/** Deletion limitation note and the last recorded history gap. */
function GitHubHistoryNotes({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { display, githubStatusQuery } = m;
  const resetAt = githubStatusQuery.data?.sync.last_reset_at;
  return (
    <>
      <p className="text-muted-foreground">{t('githubDeletionUnsupported')}</p>
      {resetAt && (
        <p className="text-muted-foreground">
          {t('githubGapRecorded')}: {formatDate(resetAt, display.locale, display.timezone)}
        </p>
      )}
    </>
  );
}

/** Per-resource sync progress lines and the invalid-cursor warning. */
function GitHubResourceProgress({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { display, githubStatusQuery } = m;
  const sync = githubStatusQuery.data?.sync;
  return (
    <>
      {sync?.resources.map((item) => (
        <p key={item.resource} className={item.incomplete ? 'error' : 'muted'}>
          {t('githubResourceProgress', { resource: item.resource, phase: item.phase, page: item.page, sweep: item.sweep_revision })}
          {item.floor && item.upper
            ? ` · ${formatDate(item.floor, display.locale, display.timezone)} – ${formatDate(item.upper, display.locale, display.timezone)}`
            : ''}
          {item.completed_upper ? ` · ${formatDate(item.completed_upper, display.locale, display.timezone)}` : ''}
        </p>
      ))}
      {sync?.cursor_invalid && <p className="error">{t('githubCursorInvalid')}</p>}
    </>
  );
}

/** Reconciliation banner and the identity of the interrupted recovery operation, when one exists. */
function GitHubRecoveryNotes({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const data = m.githubStatusQuery.data;
  return (
    <>
      {(data?.state === 'reconciliation_required' || data?.coordinator_state === 'reconciliation_required' || data?.recovery_available) && (
        <p className="error">{t('githubReconciliation')}</p>
      )}
      {data?.recovery_available && data.operation_source_id && (
        <p className="text-muted-foreground">
          {data.operation_source_id}
          {data.operation_source_generation ? ` · generation ${data.operation_source_generation}` : ''}
          {data.operation_configuration_revision !== null ? ` · revision ${data.operation_configuration_revision}` : ''}
        </p>
      )}
    </>
  );
}

/** Revoke-uncertainty warning and the status load failure line. */
function GitHubFailureNotes({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { githubStatusQuery } = m;
  return (
    <>
      {['provider_revoke_outcome_unknown', 'provider_revoke_pending'].includes(githubStatusQuery.data?.coordinator_error_code ?? '') && (
        <p className="error">{t('githubRevokeUnknown')}</p>
      )}
      {githubStatusQuery.isError && <p className="error">{t('githubConnectionFailed')}</p>}
    </>
  );
}

/** Retry of an unknown/pending provider revoke goes through the peer-review disconnect dialog. */
function RetryRevokeControl({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { locked, dirty, busyAction, reviewGitHubDisconnect } = m;
  return (
    <Button variant="outline" disabled={locked || dirty} onClick={reviewGitHubDisconnect}>
      {busyAction === 'github-peers' ? t('loading') : t('githubRetryRevoke')}
    </Button>
  );
}

/** Opens the disclosure for resuming an interrupted authorization or refresh. */
function ReviewRecoveryControl({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { githubStatusQuery, locked, dirty, sourceStatus, revision, reviewGitHubRecovery } = m;
  return (
    <Button
      variant="outline"
      disabled={locked || dirty || sourceStatus !== 'active' || revision < 1 || !githubStatusQuery.data?.operation_source_id}
      onClick={reviewGitHubRecovery}
    >
      {t('githubReviewRecovery')}
    </Button>
  );
}

/** Shown while the coordinator is busy with another operation. */
function WaitControl() {
  const t = useTranslations('sources');
  return (
    <p className="text-muted-foreground" role="status">
      {t('githubRecoveryWait')}
    </p>
  );
}

/** Refresh and disconnect for a ready connection. */
function ReadyControls({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { locked, dirty, busyAction, refreshGitHub, reviewGitHubDisconnect } = m;
  return (
    <>
      <Button variant="outline" disabled={locked || dirty} onClick={refreshGitHub}>
        {busyAction === 'github-refresh' ? t('saving') : t('githubRefresh')}
      </Button>
      <Button variant="outline" disabled={locked || dirty} onClick={reviewGitHubDisconnect}>
        {busyAction === 'github-peers' ? t('loading') : t('githubDisconnect')}
      </Button>
    </>
  );
}

/** Starts the GitHub authorization for a saved, active source. */
function ConnectControl({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { locked, dirty, sourceStatus, revision, busyAction, connectGitHub } = m;
  return (
    <Button variant="outline" disabled={locked || dirty || sourceStatus !== 'active' || revision < 1} onClick={connectGitHub}>
      {busyAction === 'github-connect' ? t('loading') : t('githubConnect')}
    </Button>
  );
}

/** Control component per mode; a new coordinator state plugs in by adding an entry and a `githubControlMode` branch. */
const MODE_CONTROLS = {
  retryRevoke: RetryRevokeControl,
  reviewRecovery: ReviewRecoveryControl,
  wait: WaitControl,
  ready: ReadyControls,
  connect: ConnectControl,
} as const;

/** The mode-dependent connect / refresh / disconnect / recovery control set (see githubControlMode). */
function GitHubModeControls({ m }: { m: ConnectorEditorModel }) {
  const Control = MODE_CONTROLS[githubControlMode(m.githubStatusQuery.data)];
  return <Control m={m} />;
}

/** Reset-history button (when a scan scope exists) followed by the mode controls. */
function GitHubActions({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, githubStatusQuery, locked, dirty, sourceStatus, revision, busyAction, resetGitHubHistory } = m;
  return (
    <div className="form-actions">
      {provider === 'github' && githubStatusQuery.data?.sync.scope_sha256 && (
        <Button variant="outline" disabled={locked || dirty || sourceStatus !== 'active' || revision < 1} onClick={resetGitHubHistory}>
          {busyAction === 'github-reset' ? t('saving') : t('githubResetHistory')}
        </Button>
      )}
      <GitHubModeControls m={m} />
    </div>
  );
}

/** GitHub connection status, sync progress, recovery and connect/disconnect controls. */
export function GitHubSection({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, sourceId, githubStatusQuery, showStep } = m;
  return (
    <>
      {showStep('connect', 'verify') && provider === 'github' && (
        <SettingsSection title={githubStatusQuery.data?.state === 'ready' ? t('githubConnected') : t('githubNotConnected')}>
          <GitHubWindowNotes m={m} />
          <GitHubWarningNotes m={m} />
          <GitHubHistoryNotes m={m} />
          <GitHubResourceProgress m={m} />
          <GitHubRecoveryNotes m={m} />
          <GitHubFailureNotes m={m} />
          {sourceId && <GitHubSummary sourceId={sourceId} />}
          <GitHubActions m={m} />
        </SettingsSection>
      )}
    </>
  );
}
