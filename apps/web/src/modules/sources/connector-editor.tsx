'use client';

import { CheckboxField, FieldGrid, SettingsActions, SettingsSection } from '@/components/settings-layout';
import { useTranslations } from 'next-intl';
import { useEffect } from 'react';
import { Badge } from '@/components/ui/badge';
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
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { DisconnectDialog } from './disconnect-dialog';
import { RenameSource } from './source-rename';
import { SyncHistory } from './sync-history';
import { useSourceActions } from './use-source-actions';
import { formatDateTime as formatDate } from '@/core/format';
import { BuiltinOptions } from './builtin-options';
import { ProviderGuide, ProviderTerms, ScopeHint } from './provider-scope';
import { McpCollectionEditor } from './mcp-collection-editor';
import {
  ExecutionBackend,
  ConnectorConfig,
  connectorKeys,
  allowedIntervals,
  minIntervalMinutes,
  ConnectorActivation,
  RealtimeMode,
  Source,
} from './api';
import { useConnectorEditor, type ConnectorEditorProps } from './use-connector-editor';
import type { ConnectorEditorModel } from './connector-editor-types';
import { GitHubSection } from './connector-editor-github';
import { CONNECT_SECTIONS, connectFamily, SCOPE_SECTIONS, scopeFamily } from './connector-section-registry';
import { intervals, isPreset, isNativeEntry, isProvider, providerKey, errorText, FIELD_LABEL_KEYS } from './connector-editor-model';

export { providerCredentialRequired } from './connector-editor-model';
export type { EditorStep, VerifyState, EditorSnapshot, EditorActions } from './connector-editor-model';

/** Pause and Disconnect controls for the editor header, sharing handlers and the confirmation dialog with the source list rows. */
function EditorSourceActions({
  source,
  activation,
  disabled,
  onChanged,
  onPurgeStarted,
  onClose,
  onBusyChange,
}: {
  source: Source;
  activation?: ConnectorActivation;
  disabled: boolean;
  onChanged: () => void;
  onPurgeStarted?: (operationId: string) => void;
  onClose: () => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const t = useTranslations('sources');
  const actions = useSourceActions({
    source,
    activation,
    onChanged: () => {
      onChanged();
      onClose();
    },
    onPurgeStarted,
  });
  const { busy: actionsBusy } = actions;
  useEffect(() => {
    onBusyChange(actionsBusy);
    return () => onBusyChange(false);
  }, [actionsBusy, onBusyChange]);
  const off = disabled || actions.busy;
  return (
    <>
      {source.status === 'active' && (
        <Button variant="outline" disabled={off} onClick={() => void actions.toggleStatus()}>
          {actions.busy ? t('pausing') : t('pause')}
        </Button>
      )}
      <DisconnectDialog
        sourceId={source.id}
        name={source.name}
        archived={source.status === 'archived'}
        disabled={off}
        onConfirm={(deleteData) => void actions.disconnect(deleteData)}
        trigger={
          <Button variant="outline" aria-disabled={off}>
            {actions.busy ? t('disconnecting') : t(source.status === 'archived' ? 'disconnectDelete' : 'disconnectAction')}
          </Button>
        }
      />
      {actions.error && (
        <p className="error" role="alert">
          {t('actionFailed')}
        </p>
      )}
    </>
  );
}
/** Layout classes of a provider tile in the add grid. */
const PROVIDER_TILE_CLASS =
  'source-provider h-auto min-h-14 min-w-0 flex-col items-start justify-center overflow-hidden whitespace-normal break-words text-left font-normal';

/** Provider step intro: catalog loading/error, shape-filter empty state, search box and no-match notice. */
function ProviderStepSearch({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { providerQuery, setProviderQuery, sourceId, catalogQuery, catalog, embedded, providerGroups, shapeFilter, onClearFilter } = m;
  return (
    <>
      {!embedded && (
        <header className="section-heading">
          <div>
            <h2>{t('setupTitle')}</h2>
            <p className="text-muted-foreground">{t('stepConnect')}</p>
          </div>
        </header>
      )}
      {catalogQuery.isPending && <p className="text-muted-foreground">{t('loading')}</p>}
      {catalogQuery.isError && (
        <p className="error" role="alert">
          {t('loadFailed')}{' '}
          <Button variant="outline" onClick={() => catalogQuery.refetch()}>
            {t('retry')}
          </Button>
        </p>
      )}
      {Boolean(shapeFilter?.length) && catalogQuery.isSuccess && !sourceId && !catalog.some((entry) => entry.kind !== 'group') && (
        <p role="status" className="text-muted-foreground">
          {t('shapeFilterNone')}{' '}
          {onClearFilter && (
            <Button variant="outline" size="sm" onClick={onClearFilter}>
              {t('shapeFilterClear')}
            </Button>
          )}
        </p>
      )}
      {catalog.length > 0 && (
        <div className="field">
          <Label htmlFor="source-provider-search">{t('providerSearchLabel')}</Label>
          <Input
            id="source-provider-search"
            type="search"
            autoFocus
            autoComplete="off"
            placeholder={t('providerSearchPlaceholder')}
            value={providerQuery}
            onChange={(event) => setProviderQuery(event.target.value)}
          />
        </div>
      )}
      {catalog.length > 0 && providerGroups.length === 0 && (
        <p role="status" className="text-muted-foreground">
          {t('providerSearchNone')}
        </p>
      )}
    </>
  );
}

/** Grouped catalog provider buttons; picking one selects the provider and its default configuration. */
function ProviderGroups({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, sourceId, addedBuiltinIds, selectProvider, locked, providerGroupTitleKey, providerGroups, catalogStatus } = m;
  return (
    <>
      {providerGroups.map(({ key: group, entries }) => (
        <SettingsSection key={group} title={t(providerGroupTitleKey(group))}>
          <div
            className="source-provider-grid !m-0 !grid-cols-[repeat(auto-fill,minmax(14rem,1fr))]"
            role="group"
            aria-label={t(providerGroupTitleKey(group))}
          >
            {entries.map((entry) => {
              const localProvider = entry.provider_id;
              const supported =
                Boolean(entry.source_type && entry.code_available) &&
                ['available', 'implemented', 'requires_credentials'].includes(entry.availability) &&
                isProvider(localProvider);
              const added = addedBuiltinIds.has(localProvider);
              return (
                <Button
                  key={entry.provider_id}
                  type="button"
                  variant="outline"
                  className={`${PROVIDER_TILE_CLASS}${provider === localProvider ? ' is-selected' : ''}`}
                  disabled={!supported || locked || added || Boolean(sourceId)}
                  onClick={() => {
                    if (!supported || !isProvider(localProvider)) return;
                    selectProvider(localProvider, entry);
                  }}
                >
                  <strong>{providerKey(entry.provider_id) === 'provider' ? entry.label : t(providerKey(entry.provider_id))}</strong>
                  {(catalogStatus(entry) || !entry.code_available) && (
                    <span>{[catalogStatus(entry), entry.code_available ? '' : t('codeUnavailable')].filter(Boolean).join(' · ')}</span>
                  )}
                  {added && <Badge variant="secondary">{t('builtinAdded')}</Badge>}
                  {!supported && <small>{t('providerUnavailableReason')}</small>}
                </Button>
              );
            })}
          </div>
        </SettingsSection>
      ))}
    </>
  );
}

/** Source name field, create error and (standalone) create button. */
function IdentityPanel({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const {
    provider,
    name,
    setName,
    sourceId,
    busyAction,
    error,
    errorDetail,
    currentEntry,
    markDraftChanged,
    closeEditor,
    createSource,
    locked,
    embedded,
    providerOk,
  } = m;
  return (
    <>
      <SettingsSection title={t('panelIdentity')} description={t('panelIdentityHint')}>
        <div className="form source-editor-form">
          <p className="text-muted-foreground">{t('selectedProvider', { provider: currentEntry?.label ?? provider })}</p>
          <div className="field">
            <Label htmlFor="new-source-name">{t('sourceName')}</Label>
            <Input
              id="new-source-name"
              maxLength={200}
              disabled={locked || Boolean(sourceId)}
              value={name}
              onChange={(event) => {
                setName(event.target.value);
                markDraftChanged();
              }}
            />
          </div>
          {!sourceId && error && (
            <p className="error" role="alert">
              {t(
                error as
                  | 'conflict'
                  | 'configurationInvalid'
                  | 'serviceUnavailable'
                  | 'sourceNameRequired'
                  | 'actionFailed'
                  | 'builtinAlreadyAdded',
              )}
              {errorDetail && ` | ${errorDetail}`}
            </p>
          )}
          {!embedded && (
            <div className="form-actions">
              <Button disabled={locked || !providerOk} onClick={() => void createSource()}>
                {busyAction === 'create' ? t('saving') : t('createAndContinue')}
              </Button>
              <Button variant="outline" onClick={closeEditor}>
                {t('cancel')}
              </Button>
            </div>
          )}
        </div>
      </SettingsSection>
    </>
  );
}

/** Provider selection step (also the standalone add form); stays mounted but hidden in other embedded steps. */
function ProviderStep({ m }: { m: ConnectorEditorModel }) {
  const { sourceId, embedded, step } = m;
  return (
    <>
      {(!sourceId || embedded) && (
        <div hidden={embedded && step !== 'provider'} className="flex flex-col gap-4">
          <ProviderStepSearch m={m} />
          <ProviderGroups m={m} />
          <IdentityPanel m={m} />
        </div>
      )}
    </>
  );
}

/** Source name row shown at the top of the connect step when the editor is embedded in the wizard. */
function EmbeddedNameRow({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { embedded, showStep, source, onChanged } = m;
  return (
    <>
      {source && embedded && (
        <div hidden={!showStep('connect')} className="flex items-center gap-2">
          <span className="text-sm font-medium">{t('sourceName')}</span>
          <RenameSource source={source} onRenamed={onChanged} />
        </div>
      )}
    </>
  );
}

/** Title with the status badge, revision and unsaved marker. */
function HeaderTitle({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { name, secret, revision, dirty, sourceStatus, activationLabel, source } = m;
  return (
    <div>
      <h2>{name || source?.name || t('editTitle')}</h2>
      <p className="text-muted-foreground">
        <span className="badge">{sourceStatus === 'paused' ? t('statusPaused') : activationLabel}</span>{' '}
        {revision > 0 ? t('editorRevision', { revision }) : t('editorRevisionNew')}
        {dirty || secret ? ` · ${t('editorUnsaved')}` : ''}
      </p>
    </div>
  );
}

/** Collect now (active, clean) and Resume (paused, non-MCP) buttons. */
function HeaderPrimaryActions({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, dirty, busyAction, sourceStatus, visibleActivationState, resumeSource, collectNow, locked } = m;
  return (
    <>
      {sourceStatus === 'active' && visibleActivationState === 'active' && !dirty && (
        <Button variant="outline" disabled={locked} onClick={collectNow}>
          {busyAction === 'collect' ? t('collecting') : t('collectNow')}
        </Button>
      )}
      {sourceStatus === 'paused' && provider !== 'mcp' && (
        <Button variant="outline" disabled={locked} onClick={resumeSource}>
          {busyAction === 'resume' ? t('resuming') : t('resume')}
        </Button>
      )}
    </>
  );
}

/** Existing-source header: title, status line and Collect now / Resume / Pause / Disconnect / Close actions. */
function EditorHeader({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const {
    secret,
    dirty,
    setActionsBusy,
    activationQuery,
    sourceStatus,
    closeEditor,
    locked,
    embedded,
    source,
    onClose,
    onChanged,
    onPurgeStarted,
  } = m;
  return (
    <>
      <EmbeddedNameRow m={m} />
      {!embedded && (
        <header className="section-heading">
          <HeaderTitle m={m} />
          <div className="form-actions">
            <HeaderPrimaryActions m={m} />
            {source && (
              <EditorSourceActions
                source={{ ...source, status: sourceStatus }}
                activation={activationQuery.data}
                disabled={locked || dirty || Boolean(secret)}
                onChanged={onChanged}
                onPurgeStarted={onPurgeStarted}
                onClose={onClose}
                onBusyChange={setActionsBusy}
              />
            )}
            <Button variant="outline" onClick={closeEditor}>
              {t('close')}
            </Button>
          </div>
        </header>
      )}
    </>
  );
}

/** MCP sources edit their calls and schedule in the dedicated collection editor. */
function McpSection({ m }: { m: ConnectorEditorModel }) {
  const { queryClient, sourceId, setDirty, sourceStatus, showStep, onChanged } = m;
  return (
    <>
      <div hidden={!showStep('connect')}>
        <McpCollectionEditor
          sourceId={sourceId}
          sourceStatus={sourceStatus}
          onDraftChange={setDirty}
          onChanged={() => {
            void queryClient.invalidateQueries({ queryKey: connectorKeys.configuration(sourceId) });
            void queryClient.invalidateQueries({ queryKey: connectorKeys.activation(sourceId) });
            onChanged();
          }}
        />
      </div>
    </>
  );
}

/** Verify-step banner when the saved configuration is not active, with a retry action. */
function ActivationIssueBanner({ m }: { m: ConnectorEditorModel }) {
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

/** Standalone stepper summarising connect, choose data and schedule progress. */
function StepperNav({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { currentStep, steps, embedded } = m;
  return (
    <>
      {!embedded && (
        <ol aria-label={t('stepperLabel')} className="m-0 grid list-none grid-cols-3 gap-3 p-0 max-[720px]:grid-cols-1">
          {steps.map((step) => (
            <li key={step.id}>
              <a
                href={`#source-step-${step.id}`}
                aria-current={currentStep === step.id ? 'step' : undefined}
                className={`flex min-h-9 pointer-coarse:min-h-11 flex-col rounded-lg border p-3 no-underline ${currentStep === step.id ? 'border-primary' : 'border-border'}`}
              >
                <strong>{step.title}</strong>
                <span className="text-muted-foreground text-sm">{step.status}</span>
              </a>
            </li>
          ))}
        </ol>
      )}
    </>
  );
}

/** Connect step: credentials and authorization sections registered for the selected provider's family. */
function ConnectStep({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { showStep, provider } = m;
  return (
    <div id="source-step-connect" hidden={!showStep('connect')} className="scroll-mt-4">
      <SettingsSection title={t('panelCredentials')} description={t('panelCredentialsHint')}>
        {CONNECT_SECTIONS[connectFamily(provider)].map((Section, index) => (
          <Section key={index} m={m} />
        ))}
      </SettingsSection>
    </div>
  );
}

/** Provider setup guide and (for presets) the terms acknowledgement. */
function GuideAndTerms({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { csrfToken, sourceId, validation, currentEntry, builtinReview, sourceStatus, validateDraft, locked, source } = m;
  return (
    <>
      {currentEntry && (
        <ProviderGuide
          entry={currentEntry}
          source={source ? { ...source, status: sourceStatus } : null}
          onTest={sourceId ? validateDraft : undefined}
          testBusy={locked}
          testPassed={Boolean(validation)}
        />
      )}
      {isPreset(currentEntry) &&
        currentEntry &&
        (sourceId ? (
          builtinReview ? (
            <p className="text-muted-foreground" role="status">
              {t('builtinUseToggle')}
            </p>
          ) : (
            <ProviderTerms sourceId={sourceId} entry={currentEntry} csrfToken={csrfToken} disabled={locked} />
          )
        ) : (
          <p className="text-muted-foreground" role="status">
            {t('termsRequired')}
          </p>
        ))}
    </>
  );
}

/** Provider-specific scope inputs, resolved from the section registry by provider family. */
function ScopeFields({ m }: { m: ConnectorEditorModel }) {
  const Section = SCOPE_SECTIONS[scopeFamily(m.provider, m.currentEntry)];
  return <Section m={m} />;
}

/** REST item path and field mapping inputs. */
function RestFields({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, configuration, changeConfiguration } = m;
  return (
    <>
      {provider === 'rest' && (
        <FieldGrid>
          <div className="field">
            <Label htmlFor="source-items-path">{t('itemsPath')}</Label>
            <Input
              id="source-items-path"
              maxLength={256}
              aria-describedby="source-hint-items_path"
              value={configuration.items_path ?? ''}
              onChange={(event) => changeConfiguration('items_path', event.target.value)}
            />
            <ScopeHint field="items_path" />
          </div>
          <div className="field">
            <Label htmlFor="source-id-field">{t('idField')}</Label>
            <Input
              id="source-id-field"
              maxLength={128}
              aria-describedby="source-hint-id_field"
              value={configuration.id_field ?? ''}
              onChange={(event) => changeConfiguration('id_field', event.target.value)}
            />
            <ScopeHint field="id_field" />
          </div>
          <div className="field">
            <Label htmlFor="source-title-field">{t('titleField')}</Label>
            <Input
              id="source-title-field"
              maxLength={128}
              aria-describedby="source-hint-title_field"
              value={configuration.title_field ?? ''}
              onChange={(event) => changeConfiguration('title_field', event.target.value)}
            />
            <ScopeHint field="title_field" />
          </div>
          <div className="field">
            <Label htmlFor="source-content-field">{t('contentField')}</Label>
            <Input
              id="source-content-field"
              maxLength={128}
              aria-describedby="source-hint-content_field"
              value={configuration.content_field ?? ''}
              onChange={(event) => changeConfiguration('content_field', event.target.value)}
            />
            <ScopeHint field="content_field" />
          </div>
          <div className="field">
            <Label htmlFor="source-updated-field">{t('updatedField')}</Label>
            <Input
              id="source-updated-field"
              maxLength={128}
              aria-describedby="source-hint-updated_field"
              value={configuration.updated_field ?? ''}
              onChange={(event) => changeConfiguration('updated_field', event.target.value)}
            />
            <ScopeHint field="updated_field" />
          </div>
        </FieldGrid>
      )}
    </>
  );
}

/** Validate button and the last validation result. */
function ValidateRow({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { display, validation, busyAction, validateDraft, locked } = m;
  return (
    <>
      {/* Validation lives with the scope/URL it checks; running it earlier always fails for scoped and generic sources. */}
      <div className="form-actions">
        <Button variant="outline" disabled={locked} onClick={validateDraft}>
          {busyAction === 'validate' ? t('validating') : t('validateDraft')}
        </Button>
      </div>
      {validation && (
        <p className="status-panel" role="status">
          {t('validationPassed', { time: formatDate(validation.at, display.locale, display.timezone) })}
          {validation.verifiedBotId ? ` ${t('verifiedBot')}: ${validation.verifiedBotId}` : ''}
          {validation.scopeVerified ? ` · ${t('scopeVerified')}` : ''}
        </p>
      )}
    </>
  );
}

/** Choose-data step: guide, scope inputs and validation. */
function ChooseStep({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { showStep } = m;
  return (
    <>
      <div id="source-step-choose" hidden={!showStep('choose')} className="scroll-mt-4">
        <SettingsSection title={t('panelScope')} description={t('panelScopeHint')}>
          <GuideAndTerms m={m} />
          <ScopeFields m={m} />
          <RestFields m={m} />
          <ValidateRow m={m} />
        </SettingsSection>
      </div>
    </>
  );
}

/** Schedule step: interval, timezone, realtime mode, built-in options and history mode. */
function ScheduleStep({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, currentEntry, configuration, builtinEntry, changeConfiguration, locked, showStep } = m;
  return (
    <>
      <div id="source-step-collect" hidden={!showStep('collect')} className="scroll-mt-4">
        <SettingsSection title={t('panelSchedule')} description={t('panelScheduleHint')}>
          <FieldGrid>
            <div className="field">
              <Label htmlFor="source-schedule">{t('schedule')}</Label>
              <Select
                value={String(configuration.schedule_interval_minutes)}
                onValueChange={(value) =>
                  changeConfiguration('schedule_interval_minutes', Number(value) as ConnectorConfig['schedule_interval_minutes'])
                }
              >
                <SelectTrigger id="source-schedule">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(provider === 'alpha_vantage' ? [1440] : allowedIntervals(intervals, currentEntry)).map((minutes) => (
                    <SelectItem key={minutes} value={String(minutes)}>
                      {t('everyMinutes', { minutes })}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {minIntervalMinutes(currentEntry) > 15 && (
                <small className="text-muted-foreground">
                  {minIntervalMinutes(currentEntry) >= 60
                    ? t('minIntervalHours', { hours: minIntervalMinutes(currentEntry) / 60 })
                    : t('minIntervalMinutes', { minutes: minIntervalMinutes(currentEntry) })}
                </small>
              )}
            </div>
            <div className="field">
              <Label htmlFor="source-timezone">{t('timezone')}</Label>
              <Input
                id="source-timezone"
                aria-describedby="source-hint-timezone"
                value={configuration.timezone}
                onChange={(event) => changeConfiguration('timezone', event.target.value)}
              />
              <ScopeHint field="timezone" />
            </div>
            <div className="field">
              <Label htmlFor="source-realtime-mode">{t('trackingModeLabel')}</Label>
              <Select
                value={configuration.realtime_mode ?? currentEntry?.realtime_tier ?? 'near_realtime'}
                onValueChange={(value) => changeConfiguration('realtime_mode', value as RealtimeMode)}
              >
                <SelectTrigger id="source-realtime-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="realtime">{t('trackingModeRealtime')}</SelectItem>
                  <SelectItem value="near_realtime">{t('trackingModeNearRealtime')}</SelectItem>
                </SelectContent>
              </Select>
              <small className="text-muted-foreground">{t('trackingModeHelp')}</small>
            </div>
          </FieldGrid>
          {builtinEntry && (
            <BuiltinOptions
              builtin={builtinEntry}
              disabled={locked}
              values={configuration}
              // An emptied field is saved as an explicit null (clears the stored value); omitted keys keep it.
              onChange={(key, value) =>
                changeConfiguration(
                  key as keyof ConnectorConfig,
                  ((Array.isArray(value) && value.length === 0) || value === undefined ? null : value) as never,
                )
              }
            />
          )}
          {isNativeEntry(currentEntry) && (
            <>
              <div className="field">
                <Label htmlFor="source-history-mode">{t('historyMode')}</Label>
                <Input
                  id="source-history-mode"
                  readOnly
                  aria-readonly="true"
                  value={t(provider === 'telegram' ? 'pendingUpdatesOnly' : 'returnedSnapshot')}
                />
              </div>
            </>
          )}
        </SettingsSection>
      </div>
    </>
  );
}

/** Advanced limits (pages, depth, timeout, JS rendering) for web and native sources. */
function AdvancedDetails({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, currentEntry, configuration, changeConfiguration } = m;
  return (
    <>
      {(provider === 'web' || isNativeEntry(currentEntry)) && (
        <details className="rounded-lg border border-border p-4">
          <summary className="cursor-pointer font-semibold">{t('advancedTitle')}</summary>
          <div className="mt-3">
            <FieldGrid>
              {provider === 'web' && (
                <>
                  <div className="field">
                    <Label htmlFor="source-max-pages">{t('maxPages')}</Label>
                    <Input
                      id="source-max-pages"
                      type="number"
                      min={1}
                      max={10}
                      value={configuration.max_pages}
                      onChange={(event) => changeConfiguration('max_pages', Number(event.target.value))}
                    />
                  </div>
                  <div className="field">
                    <Label htmlFor="source-max-depth">{t('maxDepth')}</Label>
                    <Input
                      id="source-max-depth"
                      type="number"
                      min={0}
                      max={2}
                      value={configuration.max_depth}
                      onChange={(event) => changeConfiguration('max_depth', Number(event.target.value))}
                    />
                  </div>
                  <div className="field">
                    <Label htmlFor="source-timeout">{t('timeout')}</Label>
                    <Input
                      id="source-timeout"
                      type="number"
                      min={1}
                      max={60}
                      value={configuration.timeout_seconds}
                      onChange={(event) => changeConfiguration('timeout_seconds', Number(event.target.value))}
                    />
                  </div>
                  <CheckboxField>
                    <Checkbox
                      id="source-js-render"
                      checked={configuration.js_render}
                      onCheckedChange={(checked) => changeConfiguration('js_render', checked === true)}
                    />
                    <Label htmlFor="source-js-render">{t('renderJavascript')}</Label>
                  </CheckboxField>
                </>
              )}
              {isNativeEntry(currentEntry) && (
                <>
                  <div className="field">
                    <Label htmlFor="source-native-timeout">{t('timeout')}</Label>
                    <Input
                      id="source-native-timeout"
                      type="number"
                      min={1}
                      max={30}
                      value={configuration.timeout_seconds}
                      onChange={(event) => changeConfiguration('timeout_seconds', Number(event.target.value))}
                    />
                  </div>
                </>
              )}
            </FieldGrid>
          </div>
        </details>
      )}
    </>
  );
}

/** Owner-only execution backend choice. */
function BackendDetails({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { backendChoice, setBackendChoice, workspace, showStep } = m;
  return (
    <>
      {showStep('collect') && workspace.isOwner && (
        <details className="rounded-lg border border-border p-4">
          <summary className="cursor-pointer font-semibold">{t('backendAdvanced')}</summary>
          <div className="mt-3 grid gap-3">
            <div className="field">
              <Label htmlFor="source-backend">{t('backendLabel')}</Label>
              <Select
                value={backendChoice ?? 'native'}
                onValueChange={(value) => {
                  setBackendChoice(value as ExecutionBackend);
                }}
              >
                <SelectTrigger id="source-backend">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="native">{t('backendNative')}</SelectItem>
                  <SelectItem value="n8n">{t('backendN8n')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {(backendChoice ?? 'native') === 'n8n' && (
              <p className="text-muted-foreground" role="note">
                {t('backendN8nPrereq')}
              </p>
            )}
          </div>
        </details>
      )}
    </>
  );
}

/** Advanced settings panel shown with the schedule step. */
function AdvancedSection({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, workspace, currentEntry, showStep } = m;
  return (
    <>
      {showStep('collect') && (provider === 'web' || isNativeEntry(currentEntry) || workspace.isOwner) && (
        <SettingsSection title={t('panelAdvanced')} description={t('panelAdvancedHint')}>
          <AdvancedDetails m={m} />
          <BackendDetails m={m} />
        </SettingsSection>
      )}
    </>
  );
}

/** The editable fieldset: connect, choose-data, schedule and advanced sections (locked while a request runs). */
function EditorFields({ m }: { m: ConnectorEditorModel }) {
  const { locked } = m;
  return (
    <>
      <fieldset className="source-editor-fields" disabled={locked}>
        <div className="form source-editor-form">
          <ConnectStep m={m} />
          <ChooseStep m={m} />
          <ScheduleStep m={m} />
          <AdvancedSection m={m} />
        </div>
      </fieldset>
    </>
  );
}

/** History description, Open-Meteo terms and provider quota notes. */
function ChooseNotes({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, currentEntry, historyDescription, showStep } = m;
  return (
    <>
      {showStep('choose') && historyDescription && (
        <SettingsSection title={t('history')}>
          <p className="m-0 text-sm">{historyDescription}</p>
          {isNativeEntry(currentEntry) && <p className="m-0 text-sm text-muted-foreground">{t('providerHistoryCaveat')}</p>}
        </SettingsSection>
      )}
      {showStep('choose') && provider === 'open_meteo' && (
        <SettingsSection title={t('providerTerms')}>
          <p className="m-0 text-sm">{t('openMeteoTerms')}</p>
        </SettingsSection>
      )}
    </>
  );
}

/** Provider quota limits. */
function QuotaSection({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { currentEntry, showStep } = m;
  return (
    <>
      {showStep('choose') && currentEntry?.quota_limits && (
        <SettingsSection title={t('quota')}>
          <p className="m-0 text-sm">
            {Object.entries(currentEntry.quota_limits)
              .map(([key, value]) => `${key}: ${value}`)
              .join(' · ')}
          </p>
        </SettingsSection>
      )}
    </>
  );
}

/** Error, per-field scope issues and notice lines. */
function EditorMessages({ m }: { m: ConnectorEditorModel }) {
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
function ConflictSection({ m }: { m: ConnectorEditorModel }) {
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
function TermsNotice({ m }: { m: ConnectorEditorModel }) {
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
function EditorFooterActions({ m }: { m: ConnectorEditorModel }) {
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
function GitHubDisconnectDialog({ m }: { m: ConnectorEditorModel }) {
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
function GitHubRecoveryDialog({ m }: { m: ConnectorEditorModel }) {
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

const ACTIVATION_STATE_TITLE_KEY: Record<string, string> = {
  queued: 'stateQueued',
  provisioning: 'stateProvisioning',
  saved_not_active: 'stateSavedNotActive',
  reconciliation_required: 'stateReconciliationRequired',
  disabled: 'stateDisabled',
  active: 'active',
};

/** Activation diagnostics lines: refresh failure, error code, saved-inactive and pending credential cleanup notes. */
function ActivationNotes({ m }: { m: ConnectorEditorModel }) {
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
function RemoveCredentialButton({ m }: { m: ConnectorEditorModel }) {
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
function activationNeedsReport(m: ConnectorEditorModel): boolean {
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
function ActivationDetails({ m }: { m: ConnectorEditorModel }) {
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
function EditorHistory({ m }: { m: ConnectorEditorModel }) {
  const { configuration, sourceStatus, embedded, source } = m;
  return (
    <>
      {!embedded && source && (
        <SyncHistory source={{ ...source, status: sourceStatus }} intervalMinutes={configuration.schedule_interval_minutes} />
      )}
    </>
  );
}

/** Everything under the configuration once loaded, in the original render order. */
function ConfigurationBody({ m }: { m: ConnectorEditorModel }) {
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
function VerifyPhaseMessages({ m }: { m: ConnectorEditorModel }) {
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
function VerifyStep({ m }: { m: ConnectorEditorModel }) {
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
function ConfiguredEditor({ m }: { m: ConnectorEditorModel }) {
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
function SourceEditorBody({ m }: { m: ConnectorEditorModel }) {
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

/** Edit and activate connectors through owner APIs; local FHIR/Home Assistant scopes use encrypted bearer tokens, while GitHub uncertainty and retained history stay explicit. */
export function ConnectorEditor(props: ConnectorEditorProps) {
  const m = useConnectorEditor(props);
  const t = useTranslations('sources');
  const { sourceId, embedded } = m;
  return (
    <section
      className={
        embedded ? 'source-editor flex flex-col gap-4' : 'source-editor flex flex-col gap-4 rounded-lg border border-border bg-card p-5'
      }
      aria-label={sourceId ? t('editTitle') : t('setupTitle')}
    >
      <ProviderStep m={m} />
      <SourceEditorBody m={m} />
    </section>
  );
}
