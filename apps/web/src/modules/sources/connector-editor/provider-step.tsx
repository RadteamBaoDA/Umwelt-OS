'use client';

import type { ConnectorEditorModel } from '../connector-editor-types';
import { SettingsSection } from '@/components/settings-layout';
import { useTranslations } from 'next-intl';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { isProvider, providerKey } from '../connector-editor-model';

/** Layout classes of a provider tile in the add grid. */
export const PROVIDER_TILE_CLASS =
  'source-provider h-auto min-h-14 min-w-0 flex-col items-start justify-center overflow-hidden whitespace-normal break-words text-left font-normal';

/** Provider step intro: catalog loading/error, shape-filter empty state, search box and no-match notice. */
export function ProviderStepSearch({ m }: { m: ConnectorEditorModel }) {
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
export function ProviderGroups({ m }: { m: ConnectorEditorModel }) {
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
export function IdentityPanel({ m }: { m: ConnectorEditorModel }) {
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
export function ProviderStep({ m }: { m: ConnectorEditorModel }) {
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
