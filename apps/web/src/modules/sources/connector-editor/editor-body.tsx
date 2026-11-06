'use client';

import type { ConnectorEditorModel } from '../connector-editor-types';
import { SettingsSection } from '@/components/settings-layout';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { GitHubSection } from '../connector-editor-github';
import { errorText } from '../connector-editor-model';
import { EditorHeader } from './editor-header';
import { McpSection, StepperNav, ChooseNotes } from './connect-choose-steps';
import {
  ActivationIssueBanner,
  EditorMessages,
  ConflictSection,
  TermsNotice,
  EditorFooterActions,
  GitHubDisconnectDialog,
  GitHubRecoveryDialog,
  ActivationDetails,
  EditorHistory,
} from './editor-messages';
import { EditorFields, QuotaSection } from './schedule-step';

/** Everything under the configuration once loaded, in the original render order. */
export function ConfigurationBody({ m }: { m: ConnectorEditorModel }) {
  return (
    <>
      <ActivationIssueBanner m={m} />
      <StepperNav m={m} />
      <EditorFields m={m} />
      <ChooseNotes m={m} />
      <GitHubSection m={m} />
      <QuotaSection m={m} />
      <EditorMessages m={m} />
      <ConflictSection m={m} />
      <TermsNotice m={m} />
      <EditorFooterActions m={m} />
      <GitHubDisconnectDialog m={m} />
      <GitHubRecoveryDialog m={m} />
      <ActivationDetails m={m} />
      <EditorHistory m={m} />
    </>
  );
}

/** Phase message of the verification run (saving, waiting, collecting, outcome) and the run id. */
export function VerifyPhaseMessages({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { error, errorDetail, verify } = m;
  return (
    <>
      {verify.phase === 'saving' && <p role="status">{t('saving')}</p>}
      {verify.phase === 'activating' && <p role="status">{t('verifyWaitingActivation')}</p>}
      {verify.phase === 'collecting' && <p role="status">{t('verifyCollecting')}</p>}
      {verify.phase === 'succeeded' && <p role="status">{t('verifySucceeded')}</p>}
      {verify.phase === 'no_changes' && <p role="status">{t('verifyNoChanges')}</p>}
      {verify.phase === 'failed' && (
        <p className="error" role="alert">
          {verify.code === 'local_only_source' ? t('verifyLocalOnly') : t('verifyFailed', { code: verify.code ?? 'unknown' })}
          {!error && errorDetail && ` | ${errorDetail}`}
        </p>
      )}
      {verify.runId && <p className="text-muted-foreground">{t('verifyRunId', { id: verify.runId })}</p>}
    </>
  );
}

/** Final step: save, enable and run one real collection, with progress and outcome. */
export function VerifyStep({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { verify, termsBlocked, sourceStatus, runVerify, locked, verifyBusy, step } = m;
  return (
    <>
      {step === 'verify' && (
        <SettingsSection title={t('verifyTitle')} description={t('verifyIntro')}>
          <div>
            <Button disabled={locked || verifyBusy || sourceStatus !== 'active' || termsBlocked} onClick={() => void runVerify()}>
              {verify.phase === 'failed' ? t('verifyRetry') : t('verifyRun')}
            </Button>
          </div>
          <VerifyPhaseMessages m={m} />
        </SettingsSection>
      )}
    </>
  );
}

/** Editor body once the saved configuration loaded: MCP collection editor or the generic sections, then verify. */
export function ConfiguredEditor({ m }: { m: ConnectorEditorModel }) {
  const { provider } = m;
  return (
    <>
      {provider === 'mcp' ? (
        <McpSection m={m} />
      ) : (
        <>
          <ConfigurationBody m={m} />
        </>
      )}
      <VerifyStep m={m} />
    </>
  );
}

/** Existing-source body: header, load state and the configured editor. */
export function SourceEditorBody({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { sourceId, configurationQuery } = m;
  return (
    <>
      {sourceId && (
        <>
          <EditorHeader m={m} />
          {configurationQuery.isPending && <p className="text-muted-foreground">{t('loading')}</p>}
          {configurationQuery.isError && (
            <div className="error" role="alert">
              {t(
                errorText(configurationQuery.error) as
                  'conflict' | 'configurationInvalid' | 'serviceUnavailable' | 'sourceNameRequired' | 'actionFailed',
              )}{' '}
              <Button variant="outline" onClick={() => configurationQuery.refetch()}>
                {t('retry')}
              </Button>
            </div>
          )}
          {configurationQuery.isSuccess && (
            <>
              <ConfiguredEditor m={m} />
            </>
          )}
        </>
      )}
    </>
  );
}
