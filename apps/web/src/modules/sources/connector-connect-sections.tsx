'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { PasswordInput } from '@/components/ui/password-input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { keyNeeded, usesApiKey, usesProviderCredential } from './connector-editor-model';
import type { ConnectorEditorModel } from './connector-editor-types';

/** Google OAuth connect/disconnect controls and grant status. */
export function GoogleConnect({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, sourceId, revision, providerCredentialConfigured, dirty, googleStatusQuery, connectGoogle, disconnectGoogle, locked } =
    m;
  return (
    <>
      {['google_mail', 'google_calendar', 'google_drive'].includes(provider) && (
        <div className="grid gap-3">
          <p className="text-muted-foreground">
            {googleStatusQuery.data?.state === 'ready' && providerCredentialConfigured ? t('googleConnected') : t('googleReadOnlyConsent')}
          </p>
          {googleStatusQuery.data?.error_code && (
            <p className="error" role="alert">
              {t('googleGrantNeedsAttention')}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={locked || !sourceId || dirty || revision < 1}
              onClick={() => void connectGoogle()}
            >
              {t('googleConnect')}
            </Button>
            {googleStatusQuery.data?.state === 'ready' && (
              <Button type="button" variant="outline" disabled={locked || dirty} onClick={() => void disconnectGoogle()}>
                {t('googleDisconnect')}
              </Button>
            )}
          </div>
        </div>
      )}
    </>
  );
}

/** Reddit OAuth connect/disconnect controls and grant status. */
export function RedditConnect({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, sourceId, revision, dirty, redditStatusQuery, connectReddit, disconnectReddit, locked } = m;
  return (
    <>
      {provider === 'reddit' && (
        <div className="grid gap-3">
          <p className="text-muted-foreground">
            {redditStatusQuery.data?.state === 'ready' ? t('redditConnected') : t('redditReadOnlyConsent')}
          </p>
          {redditStatusQuery.data?.error_code && (
            <p className="error" role="alert">
              {t('redditGrantNeedsAttention')}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={locked || !sourceId || dirty || revision < 1}
              onClick={() => void connectReddit()}
            >
              {t('redditConnect')}
            </Button>
            {redditStatusQuery.data?.state === 'ready' && (
              <Button type="button" variant="outline" disabled={locked || dirty} onClick={() => void disconnectReddit()}>
                {t('redditDisconnect')}
              </Button>
            )}
          </div>
        </div>
      )}
    </>
  );
}

/** Write-only bearer token field for header-authenticated providers, plus the no-credential or OAuth note. */
export function BearerTokenField({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const {
    provider,
    secret,
    setSecret,
    providerCredentialConfigured,
    setDirty,
    setValidation,
    setNotice,
    currentEntry,
    setDirtyRef,
    bumpDraftVersion,
  } = m;
  return (
    <>
      {currentEntry?.auth_methods.includes('http_header') && (
        <div className="field">
          <Label htmlFor="source-local-bearer-token">
            {t(provider === 'notion' ? 'notionIntegrationToken' : provider === 'slack' ? 'slackBotToken' : 'localBearerToken')}
          </Label>
          <PasswordInput
            id="source-local-bearer-token"
            autoComplete="new-password"
            maxLength={512}
            required={!providerCredentialConfigured}
            value={secret}
            onChange={(event) => {
              setSecret(event.target.value);
              setDirtyRef(true);
              setDirty(true);
              bumpDraftVersion();
              setValidation(null);
              setNotice('');
            }}
          />
          <small className="text-muted-foreground">{providerCredentialConfigured ? t('credentialSavedWriteOnly') : t('secretHelp')}</small>
        </div>
      )}
      {!['telegram', 'alpha_vantage', 'rest', 'google_mail', 'google_calendar', 'google_drive', 'reddit'].includes(provider) &&
        !usesProviderCredential(currentEntry) &&
        currentEntry &&
        (currentEntry.auth_methods.every((method) => method === 'none') ? (
          <p className="text-muted-foreground">{t('connectNoCredential')}</p>
        ) : (
          currentEntry.auth_methods.includes('oauth2') && <p className="text-muted-foreground">{t('connectOAuth')}</p>
        ))}
    </>
  );
}

/** Telegram bot token keep/replace controls. */
export function TelegramCredential({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const {
    provider,
    secret,
    setSecret,
    telegramSecretAction,
    setTelegramSecretAction,
    dirty,
    setDirty,
    setValidation,
    setNotice,
    setDirtyRef,
    bumpDraftVersion,
  } = m;
  return (
    <>
      {provider === 'telegram' && (
        <>
          <div className="field">
            <Label htmlFor="source-telegram-secret-action">{t('telegramCredentialAction')}</Label>
            <Select
              value={telegramSecretAction}
              onValueChange={(value) => {
                setTelegramSecretAction(value as 'keep' | 'replace');
                setSecret('');
                setDirtyRef(dirty);
                bumpDraftVersion();
                setValidation(null);
                setNotice('');
              }}
            >
              <SelectTrigger id="source-telegram-secret-action">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="keep">{t('keepCredential')}</SelectItem>
                <SelectItem value="replace">{t('replaceCredential')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {telegramSecretAction === 'replace' && (
            <div className="field">
              <Label htmlFor="source-telegram-token">{t('telegramBotToken')}</Label>
              <PasswordInput
                id="source-telegram-token"
                autoComplete="new-password"
                maxLength={512}
                required
                value={secret}
                onChange={(event) => {
                  setSecret(event.target.value);
                  setDirtyRef(true);
                  setDirty(true);
                  bumpDraftVersion();
                  setValidation(null);
                  setNotice('');
                }}
              />
              <small className="text-muted-foreground">{t('secretHelp')}</small>
            </div>
          )}
        </>
      )}
    </>
  );
}

/** Write-only API key field with status and removal for keyed providers. */
export function ApiKeyField({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const {
    provider,
    secret,
    setSecret,
    backendChoice,
    providerCredentialConfigured,
    setDirty,
    setValidation,
    setNotice,
    currentEntry,
    visibleActivationError,
    setDirtyRef,
    bumpDraftVersion,
  } = m;
  return (
    <>
      {(provider === 'alpha_vantage' || (usesApiKey(currentEntry) && (backendChoice ?? 'native') === 'native')) && (
        <div className="field">
          <Label htmlFor="source-provider-key">
            {t(provider === 'coingecko' ? 'coingeckoKey' : provider === 'alpha_vantage' ? 'alphaVantageKey' : 'providerKey')}
          </Label>
          <PasswordInput
            id="source-provider-key"
            autoComplete="new-password"
            maxLength={256}
            required={!providerCredentialConfigured}
            value={secret}
            onChange={(event) => {
              setSecret(event.target.value);
              setDirtyRef(true);
              setDirty(true);
              bumpDraftVersion();
              setValidation(null);
              setNotice('');
            }}
          />
          <small className="text-muted-foreground">{providerCredentialConfigured ? t('credentialSavedWriteOnly') : t('secretHelp')}</small>
          {usesApiKey(currentEntry) && keyNeeded(visibleActivationError) && (
            <small className="error" role="alert">
              {t('providerKeyNeeded')}
            </small>
          )}
        </div>
      )}
    </>
  );
}

/** REST authentication method and header credential fields. */
export function RestAuthFields({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const {
    provider,
    authMethod,
    setAuthMethod,
    authHeaderName,
    setAuthHeaderName,
    secret,
    setSecret,
    dirty,
    setValidation,
    markDraftChanged,
    setDirtyRef,
    bumpDraftVersion,
  } = m;
  return (
    <>
      {provider === 'rest' && (
        <>
          <div className="field">
            <Label htmlFor="source-auth-method">{t('auth')}</Label>
            <Select
              value={authMethod}
              onValueChange={(value) => {
                setAuthMethod(value as typeof authMethod);
                markDraftChanged();
                setValidation(null);
              }}
            >
              <SelectTrigger id="source-auth-method">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">{t('noAuthentication')}</SelectItem>
                <SelectItem value="http_header">{t('headerAuthentication')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {authMethod === 'http_header' && (
            <>
              <div className="field">
                <Label htmlFor="source-header-name">{t('headerName')}</Label>
                <Input
                  id="source-header-name"
                  maxLength={128}
                  value={authHeaderName}
                  onChange={(event) => {
                    setAuthHeaderName(event.target.value);
                    markDraftChanged();
                    setValidation(null);
                  }}
                />
              </div>
              <div className="field">
                <Label htmlFor="source-provider-secret">{t('providerSecret')}</Label>
                <PasswordInput
                  id="source-provider-secret"
                  autoComplete="new-password"
                  value={secret}
                  onChange={(event) => {
                    setSecret(event.target.value);
                    setDirtyRef(dirty || Boolean(event.target.value));
                    bumpDraftVersion();
                    setValidation(null);
                  }}
                />
                <small className="text-muted-foreground">{t('secretHelp')}</small>
              </div>
            </>
          )}
        </>
      )}
    </>
  );
}
