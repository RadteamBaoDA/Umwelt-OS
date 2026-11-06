'use client';

import type { ConnectorEditorModel } from '../connector-editor-types';
import { SettingsActions, SettingsSection } from '@/components/settings-layout';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { SyncHistory } from '../sync-history';
import { FIELD_LABEL_KEYS } from '../connector-editor-model';

/** Verify-step banner when the saved configuration is not active, with a retry action. */
export function ActivationIssueBanner({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const {
    revision,
    dirty,
    busyAction,
    termsBlocked,
    visibleActivationState,
    visibleActivationError,
    retryActivation,
    locked,
    showStep,
    verifyBusy,
  } = m;
  return (
    <>
      {/* A verify run saves (saved_not_active) then activates (queued); hide interim activation state until it settles. */}
      {showStep('verify') &&
        revision > 0 &&
        !verifyBusy &&
        (visibleActivationState === 'saved_not_active' ||
          visibleActivationState === 'reconciliation_required' ||
          Boolean(visibleActivationError)) && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive p-4" role="status">
            <p>
              <strong>{t('activationIssueTitle')}</strong> {t('savedInactive')}
              {visibleActivationError ? ` ${t('activationError')}: ${visibleActivationError}` : ''}
            </p>
            {!dirty && (
              <Button variant="outline" disabled={locked || termsBlocked} onClick={retryActivation}>
                {busyAction === 'activate' ? t('enabling') : t('retryActivation')}
              </Button>
            )}
          </div>
        )}
    </>
  );
}

/** Error, per-field scope issues and notice lines. */
export function EditorMessages({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { issues, error, errorDetail, notice } = m;
  return (
    <>
      {error && (
        <p className="error" role="alert">
          {error === 'configurationInvalid' && issues.length > 0
            ? t('issuesTitle')
            : t(
                error as
                  | 'conflict'
                  | 'configurationInvalid'
                  | 'serviceUnavailable'
                  | 'validationOutcomeUnknown'
                  | 'sourceNameRequired'
                  | 'actionFailed'
                  | 'reloadFailed'
                  | 'builtinAlreadyAdded'
                  | 'redditConnectionFailed'
                  | 'redditDisconnectFailed',
              )}
          {errorDetail && ` | ${errorDetail}`}
        </p>
      )}
      {/* Each rejected field by its form label, so the owner knows exactly which input to fix. */}
      {issues.length > 0 && (
        <ul className="error m-0 list-disc pl-5" data-testid="scope-issues">
          {issues.map((issue) => (
            <li key={issue.field}>
              {t.has(FIELD_LABEL_KEYS[issue.field] ?? '') ? t(FIELD_LABEL_KEYS[issue.field] as 'history') : issue.field}:{' '}
              {t(
                (
                  {
                    required: 'issueRequired',
                    format: 'issueFormat',
                    tooLong: 'issueTooLong',
                    range: 'issueRange',
                    invalid: 'issueInvalid',
                  } as const
                )[issue.reason],
              )}
            </li>
          ))}
        </ul>
      )}
      {notice && (
        <p className="text-muted-foreground" role="status">
          {notice}
        </p>
      )}
    </>
  );
}

/** Configuration conflict notice with reload action. */
export function ConflictSection({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { conflict, reloadServerConfiguration, locked } = m;
  return (
    <>
      {conflict && (
        <SettingsSection title={t('conflict')}>
          <div role="group" aria-label={t('conflict')}>
            <Button variant="outline" disabled={locked} onClick={reloadServerConfiguration}>
              {t('reloadDiscard')}
            </Button>
          </div>
        </SettingsSection>
      )}
    </>
  );
}

/** Notice that terms must be accepted first. */
export function TermsNotice({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { termsBlocked, visibleActivationError, showStep } = m;
  return (
    <>
      {showStep('choose', 'verify') && (termsBlocked || visibleActivationError === 'terms_not_accepted') && (
        <p className="error" role="status">
          {t('termsActivationBlocked')}
        </p>
      )}
    </>
  );
}

/** Standalone Cancel / Save / Save and enable actions. */
export function EditorFooterActions({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { busyAction, termsBlocked, sourceStatus, closeEditor, saveConfiguration, saveAndEnable, locked, embedded } = m;
  return (
    <>
      {!embedded && (
        <SettingsActions>
          <Button variant="outline" onClick={closeEditor}>
            {t('cancel')}
          </Button>
          <Button variant="outline" disabled={locked || sourceStatus !== 'active'} onClick={() => saveConfiguration()}>
            {busyAction === 'save' ? t('saving') : t('save')}
          </Button>
          <Button disabled={locked || sourceStatus !== 'active' || termsBlocked} onClick={() => void saveAndEnable()}>
            {busyAction === 'activate' ? t('enabling') : t('saveEnable')}
          </Button>
        </SettingsActions>
      )}
    </>
  );
}

/** Peer-review confirmation for disconnecting the GitHub authorization. */
export function GitHubDisconnectDialog({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { busyAction, githubPeers, githubDisconnectOpen, setGithubDisconnectOpen, confirmGitHubDisconnect, locked } = m;
  return (
    <>
      <AlertDialog open={githubDisconnectOpen} onOpenChange={setGithubDisconnectOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('githubDisconnect')}</AlertDialogTitle>
            <AlertDialogDescription>{t('githubPeerReview', { count: githubPeers.length })}</AlertDialogDescription>
          </AlertDialogHeader>
          <div>
            <strong>{t('githubPeerList')}</strong>
            <ul>
              {githubPeers.map((peer) => (
                <li key={peer.source_id}>{peer.source_id}</li>
              ))}
            </ul>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={locked}>{t('cancel')}</AlertDialogCancel>
            <Button disabled={locked} onClick={confirmGitHubDisconnect}>
              {busyAction === 'github-disconnect' ? t('saving') : t('githubDisconnect')}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

/** Disclosure confirmation for resuming an interrupted GitHub authorization or refresh. */
export function GitHubRecoveryDialog({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const {
    busyAction,
    githubRecoveryReview,
    setGithubRecoveryReview,
    githubRecoveryOpen,
    setGithubRecoveryOpen,
    confirmGitHubRecovery,
    locked,
  } = m;
  return (
    <>
      <AlertDialog
        open={githubRecoveryOpen}
        onOpenChange={(open) => {
          setGithubRecoveryOpen(open);
          if (!open) setGithubRecoveryReview(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('githubRecoveryTitle')}</AlertDialogTitle>
            {githubRecoveryReview && (
              <AlertDialogDescription>
                {t('githubRecoveryDisclosure', {
                  operationId: githubRecoveryReview.operationId,
                  kind: t(githubRecoveryReview.kind === 'authorization' ? 'githubRecoveryAuthorization' : 'githubRecoveryRefresh'),
                  sourceId: githubRecoveryReview.originSourceId,
                  generation: githubRecoveryReview.originGeneration,
                  revision: githubRecoveryReview.originRevision,
                })}
              </AlertDialogDescription>
            )}
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={locked}>{t('cancel')}</AlertDialogCancel>
            <Button disabled={locked || !githubRecoveryReview} onClick={confirmGitHubRecovery}>
              {busyAction === 'github-recovery' ? t('saving') : t('githubAcknowledgeConnect')}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

export const ACTIVATION_STATE_TITLE_KEY: Record<string, string> = {
  queued: 'stateQueued',
  provisioning: 'stateProvisioning',
  saved_not_active: 'stateSavedNotActive',
  reconciliation_required: 'stateReconciliationRequired',
  disabled: 'stateDisabled',
  active: 'active',
};

/** Activation diagnostics lines: refresh failure, error code, saved-inactive and pending credential cleanup notes. */
export function ActivationNotes({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { activationQuery, visibleActivationState, visibleActivationError } = m;
  return (
    <>
      {activationQuery.isError && <p className="error">{t('activationRefreshFailed')}</p>}
      {visibleActivationError && (
        <p className="error">
          {t('activationError')}: {visibleActivationError}
        </p>
      )}
      {visibleActivationState === 'saved_not_active' && <p className="text-muted-foreground">{t('savedInactive')}</p>}
      {visibleActivationError === 'credential_delete_pending' && (
        <p className="text-muted-foreground" role="status">
          {t('credentialPending')}
        </p>
      )}
    </>
  );
}

/** Removes the stored credential of a paused source unless a deactivation or deletion is already pending. */
export function RemoveCredentialButton({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { secret, providerCredentialConfigured, dirty, busyAction, sourceStatus, visibleActivationError, removeCredential, locked } = m;
  return (
    <>
      {providerCredentialConfigured &&
        sourceStatus === 'paused' &&
        visibleActivationError !== 'deactivation_pending' &&
        visibleActivationError !== 'credential_delete_pending' && (
          <Button variant="outline" disabled={locked || dirty || Boolean(secret)} onClick={removeCredential}>
            {busyAction === 'remove' ? t('saving') : t('removeCredential')}
          </Button>
        )}
    </>
  );
}

/** True when the verify step has something to report: not active, a collection error, a load failure, an error code or paused. */
export function activationNeedsReport(m: ConnectorEditorModel): boolean {
  const { visibleActivationState, source, activationQuery, visibleActivationError, sourceStatus } = m;
  return (
    visibleActivationState !== 'active' ||
    Boolean(source?.collection_error_code) ||
    activationQuery.isError ||
    Boolean(visibleActivationError) ||
    sourceStatus === 'paused'
  );
}

/** Verify-step activation diagnostics and credential cleanup state. */
export function ActivationDetails({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { revision, visibleActivationState, showStep, verifyBusy, source } = m;
  return (
    <>
      {showStep('verify') && revision > 0 && !verifyBusy && activationNeedsReport(m) && (
        <SettingsSection
          title={
            <>
              {t('activationState')}:{' '}
              {visibleActivationState === 'active' && source?.collection_error_code
                ? t('activeCollectionFailed')
                : t((ACTIVATION_STATE_TITLE_KEY[visibleActivationState] ?? 'stateUnknown') as 'stateUnknown')}
            </>
          }
        >
          <ActivationNotes m={m} />
          <RemoveCredentialButton m={m} />
        </SettingsSection>
      )}
    </>
  );
}

/** Run history of the source (standalone editor only). */
export function EditorHistory({ m }: { m: ConnectorEditorModel }) {
  const { configuration, sourceStatus, embedded, source } = m;
  return (
    <>
      {!embedded && source && (
        <SyncHistory source={{ ...source, status: sourceStatus }} intervalMinutes={configuration.schedule_interval_minutes} />
      )}
    </>
  );
}
