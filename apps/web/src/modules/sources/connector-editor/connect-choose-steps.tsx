'use client';

import type { ConnectorEditorModel } from '../connector-editor-types';
import { FieldGrid, SettingsSection } from '@/components/settings-layout';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { formatDateTime as formatDate } from '@/core/format';
import { ProviderGuide, ProviderTerms, ScopeHint } from '../provider-scope';
import { McpCollectionEditor } from '../mcp-collection-editor';
import { connectorKeys } from '../api';
import { CONNECT_SECTIONS, connectFamily, SCOPE_SECTIONS, scopeFamily } from '../connector-section-registry';
import { isPreset, isNativeEntry } from '../connector-editor-model';

/** MCP sources edit their calls and schedule in the dedicated collection editor. */
export function McpSection({ m }: { m: ConnectorEditorModel }) {
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

/** Standalone stepper summarising connect, choose data and schedule progress. */
export function StepperNav({ m }: { m: ConnectorEditorModel }) {
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
export function ConnectStep({ m }: { m: ConnectorEditorModel }) {
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
export function GuideAndTerms({ m }: { m: ConnectorEditorModel }) {
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
export function ScopeFields({ m }: { m: ConnectorEditorModel }) {
  const Section = SCOPE_SECTIONS[scopeFamily(m.provider, m.currentEntry)];
  return <Section m={m} />;
}

/** REST item path and field mapping inputs. */
export function RestFields({ m }: { m: ConnectorEditorModel }) {
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
export function ValidateRow({ m }: { m: ConnectorEditorModel }) {
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
export function ChooseStep({ m }: { m: ConnectorEditorModel }) {
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

/** History description, Open-Meteo terms and provider quota notes. */
export function ChooseNotes({ m }: { m: ConnectorEditorModel }) {
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
