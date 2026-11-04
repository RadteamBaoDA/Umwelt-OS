'use client';

import { useTranslations } from 'next-intl';
import { CheckboxField, FieldGrid } from '@/components/settings-layout';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ProviderScope, ScopeHint, type NativeProvider } from './provider-scope';
import type { ConnectorEditorModel } from './connector-editor-types';

/** Per Google provider: help message key, selected resource ids from the draft, and the server-side selection maximum. */
function googleScopeInfo(provider: string, configuration: ConnectorEditorModel['configuration']) {
  if (provider === 'google_mail')
    return { helpKey: 'googleMailLabelsHelp', selectedIds: configuration.google_mail_label_ids ?? [], maximum: 10 } as const;
  if (provider === 'google_calendar')
    return { helpKey: 'googleCalendarIdsHelp', selectedIds: configuration.google_calendar_ids ?? [], maximum: 25 } as const;
  return { helpKey: 'googleDriveIdsHelp', selectedIds: configuration.google_drive_ids ?? [], maximum: 100 } as const;
}

/** Discovery button, Drive back navigation and Drive location line. */
export function GoogleDiscoveryToolbar({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, busyAction, googleResourceParent, googleResourcePath, loadGoogleResources, locked } = m;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        type="button"
        variant="outline"
        disabled={locked}
        onClick={() => void loadGoogleResources(provider === 'google_drive' ? googleResourceParent : undefined)}
      >
        {busyAction === 'google-discovery' ? t('googleLoadingResources') : t('googleLoadResources')}
      </Button>
      {provider === 'google_drive' && googleResourcePath.length > 0 && (
        <Button
          type="button"
          variant="outline"
          disabled={locked}
          onClick={() => {
            const path = googleResourcePath.slice(0, -1);
            void loadGoogleResources(path.at(-1)?.id, path);
          }}
        >
          {t('googleDriveBack')}
        </Button>
      )}
      {provider === 'google_drive' && (
        <span className="text-sm text-muted-foreground">
          {t('googleDriveLocation', { path: googleResourcePath.map((item) => item.name).join(' / ') || t('googleDriveRoot') })}
        </span>
      )}
    </div>
  );
}

/** Checkbox list of discovered Google resources; folders can be opened to drill into Drive. */
export function GoogleResourceList({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, googleResources, googleResourcePath, configuration, loadGoogleResources, toggleGoogleResource, locked } = m;
  const { selectedIds } = googleScopeInfo(provider, configuration);
  return (
    <>
      {googleResources.length > 0 && (
        <div className="grid gap-2" role="group" aria-label={t('googleResourceList')}>
          {googleResources.map((resource) => {
            const checkboxId = `google-resource-${resource.id}`;
            return (
              <div
                key={resource.id}
                className="flex min-h-9 pointer-coarse:min-h-11 items-center justify-between gap-3 rounded-md border border-border p-2"
              >
                <CheckboxField>
                  <Checkbox
                    id={checkboxId}
                    disabled={locked}
                    checked={selectedIds.includes(resource.id)}
                    onCheckedChange={(checked) => toggleGoogleResource(resource.id, checked === true)}
                  />
                  <Label htmlFor={checkboxId}>
                    {resource.name}
                    {resource.mime_type ? (
                      <span className="ml-2 text-xs text-muted-foreground">
                        {resource.kind === 'folder' ? t('googleDriveFolder') : resource.mime_type}
                      </span>
                    ) : null}
                  </Label>
                </CheckboxField>
                {resource.kind === 'folder' && (
                  <Button
                    type="button"
                    variant="outline"
                    disabled={locked}
                    onClick={() => void loadGoogleResources(resource.id, [...googleResourcePath, resource])}
                  >
                    {t('googleOpenFolder')}
                  </Button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}

/** Google resource discovery and selection (labels, calendars or Drive folders). */
export function GoogleScopePicker({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, googleResourcesTruncated, googleDiscoveryError, googleStatusQuery, configuration } = m;
  const info = googleScopeInfo(provider, configuration);
  return (
    <div className="grid gap-3">
      <p className="m-0 text-sm text-muted-foreground">{t(info.helpKey)}</p>
      {googleStatusQuery.data?.state === 'ready' ? (
        <>
          <p className="m-0 text-sm" role="status">
            {t('googleReconnectForScope')}
          </p>
          <GoogleDiscoveryToolbar m={m} />
          <p className="m-0 text-sm text-muted-foreground">
            {t('googleResourceCount', { selected: info.selectedIds.length, maximum: info.maximum })}
          </p>
          {googleDiscoveryError && (
            <p className="error" role="alert">
              {t(googleDiscoveryError === 'limit' ? 'googleSelectionLimit' : 'googleDiscoveryFailed')}
            </p>
          )}
          {googleResourcesTruncated && (
            <p className="text-muted-foreground" role="status">
              {t('googleResourcesTruncated')}
            </p>
          )}
          <GoogleResourceList m={m} />
        </>
      ) : (
        <p className="m-0 text-sm text-muted-foreground">{t('googleConnectBeforeDiscovery')}</p>
      )}
    </div>
  );
}

/** FHIR base URL and patient id fields. */
export function HealthScope({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { configuration, changeConfiguration } = m;
  return (
    <>
      <FieldGrid>
        <div className="field">
          <Label htmlFor="fhir-base-url">{t('fhirBaseUrl')}</Label>
          <Input
            id="fhir-base-url"
            type="url"
            maxLength={512}
            value={configuration.fhir_base_url ?? ''}
            onChange={(event) => changeConfiguration('fhir_base_url', event.target.value)}
          />
          <small className="text-muted-foreground">{t('localEndpointHelp')}</small>
        </div>
        <div className="field">
          <Label htmlFor="fhir-patient-id">{t('fhirPatientId')}</Label>
          <Input
            id="fhir-patient-id"
            maxLength={128}
            aria-describedby="source-hint-fhir_patient_id"
            value={configuration.fhir_patient_id ?? ''}
            onChange={(event) => changeConfiguration('fhir_patient_id', event.target.value)}
          />
          <ScopeHint field="fhir_patient_id" />
        </div>
      </FieldGrid>
    </>
  );
}

/** Home Assistant base URL and entity ids fields. */
export function HomeAssistantScope({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { configuration, changeConfiguration } = m;
  return (
    <>
      <FieldGrid>
        <div className="field">
          <Label htmlFor="ha-base-url">{t('homeAssistantBaseUrl')}</Label>
          <Input
            id="ha-base-url"
            type="url"
            maxLength={512}
            value={configuration.ha_base_url ?? ''}
            onChange={(event) => changeConfiguration('ha_base_url', event.target.value)}
          />
          <small className="text-muted-foreground">{t('localEndpointHelp')}</small>
        </div>
        <div className="field">
          <Label htmlFor="ha-entity-ids">{t('homeAssistantEntityIds')}</Label>
          <Input
            id="ha-entity-ids"
            value={(configuration.ha_entity_ids ?? []).join(', ')}
            onChange={(event) =>
              changeConfiguration(
                'ha_entity_ids',
                event.target.value
                  .split(',')
                  .map((value) => value.trim())
                  .filter(Boolean)
                  .slice(0, 500),
              )
            }
          />
          <small className="text-muted-foreground">{t('homeAssistantEntityIdsHelp')}</small>
        </div>
      </FieldGrid>
    </>
  );
}

/** Fixed-scope presets have no inputs; the catalog fixes what they collect. */
export function FixedScope() {
  const t = useTranslations('sources');
  return <p className="text-muted-foreground">{t('fixedScope')}</p>;
}

/** Registered native provider scope inputs; remounted whenever the source, revision, generation or reset epoch changes. */
export function NativeScope({ m }: { m: ConnectorEditorModel }) {
  const {
    provider,
    sourceId,
    sourceGeneration,
    scopeResetEpoch,
    revision,
    providerCredentialConfigured,
    configuration,
    changeConfiguration,
    locked,
  } = m;
  return (
    <ProviderScope
      key={`${sourceId}:${provider}:${revision}:${sourceGeneration}:${scopeResetEpoch}`}
      provider={provider as NativeProvider}
      sourceId={sourceId}
      credentialConfigured={providerCredentialConfigured}
      configuration={configuration}
      disabled={locked}
      resetEpoch={scopeResetEpoch}
      onChange={changeConfiguration}
    />
  );
}

/** Feed URL input of RSS sources. */
export function RssScope({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { configuration, changeConfiguration } = m;
  return (
    <div className="field">
      <Label htmlFor="source-feed-url">{t('sourceUrl')}</Label>
      <Input
        id="source-feed-url"
        type="url"
        aria-describedby="source-hint-feed_url"
        value={configuration.feed_url ?? ''}
        onChange={(event) => changeConfiguration('feed_url', event.target.value)}
      />
      <ScopeHint field="feed_url" />
    </div>
  );
}

/** Page or API URL input shared by web pages, REST sources and any provider without a dedicated scope section. */
export function UrlScope({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, configuration, changeConfiguration } = m;
  return (
    <div className="field">
      <Label htmlFor="source-config-url">{provider === 'rest' ? t('apiUrl') : t('pageUrl')}</Label>
      <Input
        id="source-config-url"
        type="url"
        aria-describedby="source-hint-url"
        value={configuration.url ?? ''}
        onChange={(event) => changeConfiguration('url', event.target.value)}
      />
      <ScopeHint field="url" />
    </div>
  );
}
