'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Plus } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { CheckboxField, FieldGrid, SecretField, SettingsActions, SettingsPageHeader, SettingsSection } from '@/components/settings-layout';

type Provider = 'tavily' | 'brave' | 'searxng' | 'omniroute';
type Entry = { provider: Provider; endpoint: string | null; credential_configured: boolean; destination_id: string | null };
type WebSearch = { configuration_revision: number; providers: Entry[]; allow_remote_web_search: boolean };
type DraftEntry = Entry & { key: string; removed: boolean };
type Draft = { configuration_revision: number; providers: DraftEntry[]; allow_remote_web_search: boolean };
type TestResult = {
  ok: boolean;
  error: 'credential_required' | 'network_denied' | 'provider_error' | 'timeout' | null;
  detail: number | null;
  reason?: string | null;
};

/**
 * Map a backend failure `reason` token (never secret) to localized detail text, or null when none/unknown.
 * `http_<status>` and `content_type:<mime>` carry a parameter; fixed tokens map to the literal `test.reason*` keys.
 */
function reasonText(t: ReturnType<typeof useTranslations<'webSearch'>>, reason: string | null | undefined): string | null {
  if (!reason) return null;
  if (reason.startsWith('http_')) return t('test.reasonHttp', { status: reason.slice(5) });
  if (reason.startsWith('content_type:')) return t('test.reasonContentType', { type: reason.slice(13) });
  if (reason === 'too_large') return t('test.reasonTooLarge');
  if (reason === 'invalid_json') return t('test.reasonInvalidJson');
  if (reason === 'unexpected_shape') return t('test.reasonUnexpectedShape');
  return null;
}

/** Literal catalog key per search-provider test error (no runtime key building); unknown codes read as unknown. */
const TEST_ERROR_KEYS = {
  credential_required: 'test.errorCredentialRequired',
  network_denied: 'test.errorNetworkDenied',
  provider_error: 'test.errorProviderError',
  timeout: 'test.errorTimeout',
  unknown: 'test.errorUnknown',
} as const;

type ProviderRowProps = {
  t: ReturnType<typeof useTranslations<'webSearch'>>;
  entry: DraftEntry;
  index: number;
  count: number;
  saving: boolean;
  configured: boolean;
  ready: boolean;
  testPending: boolean;
  tested: boolean;
  result: TestResult | undefined;
  isError: boolean;
  error: unknown;
  onEntry: (patch: Partial<DraftEntry>) => void;
  onTest: () => void;
  onMove: (offset: number) => void;
  onRemove: () => void;
};

/** Localized one-line outcome of a provider test (count on success, mapped error key otherwise). */
function TestFeedback({ t, tested, result, isError, error }: Pick<ProviderRowProps, 't' | 'tested' | 'result' | 'isError' | 'error'>) {
  return (
    <>
      {tested && result ? (
        <p className={result.ok ? 'm-0 text-sm text-muted-foreground' : 'error m-0'} role="status">
          {result.ok
            ? result.detail == null
              ? t('test.okNoCount')
              : t('test.ok', { count: result.detail })
            : t(
                result.error && Object.hasOwn(TEST_ERROR_KEYS, result.error)
                  ? TEST_ERROR_KEYS[result.error as keyof typeof TEST_ERROR_KEYS]
                  : 'test.errorUnknown',
              )}
        </p>
      ) : null}
      {tested && result && !result.ok && reasonText(t, result.reason) ? (
        <p className="m-0 text-sm text-muted-foreground">{reasonText(t, result.reason)}</p>
      ) : null}
      {tested && isError ? (
        <p className="error m-0" role="alert">
          {t(apiFailureKey(error) ?? 'requestFailed')}
        </p>
      ) : null}
    </>
  );
}

type RowFieldsProps = Pick<ProviderRowProps, 't' | 'entry' | 'index' | 'saving' | 'configured' | 'onEntry'>;

/** Priority badges, provider select, endpoint input and the write-only API key (omitted for SearXNG) of one provider card. */
function ProviderRowFields({ t, entry, index, saving, configured, onEntry }: RowFieldsProps) {
  return (
    <>
      <div className="flex flex-wrap items-center gap-2" data-testid="web-search-priority">
        <Badge variant={index === 0 ? 'default' : 'secondary'}>
          {index === 0 ? t('priorityDefault') : t('priorityFallback', { number: index })}
        </Badge>
        {entry.provider === 'searxng' || entry.provider === 'tavily' ? <Badge variant="outline">{t('imageSupport')}</Badge> : null}
      </div>
      <FieldGrid>
        <label className="field">
          <span className="label">{t('provider')}</span>
          <Select disabled={saving} value={entry.provider} onValueChange={(provider) => onEntry({ provider: provider as Provider })}>
            <SelectTrigger aria-label={t('providerAt', { number: index + 1 })}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="tavily">Tavily</SelectItem>
              <SelectItem value="brave">Brave</SelectItem>
              <SelectItem value="searxng">SearXNG</SelectItem>
              <SelectItem value="omniroute">OmniRoute</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label className="field">
          <span className="label">{t('endpoint')}</span>
          <Input
            type="url"
            disabled={saving}
            value={entry.endpoint ?? ''}
            placeholder={entry.provider === 'omniroute' ? 'https://<host>/v1' : undefined}
            onChange={(event) => onEntry({ endpoint: event.target.value || null })}
          />
        </label>
      </FieldGrid>
      {entry.provider !== 'searxng' ? (
        <SecretField
          id={`web-search-key-${index}`}
          label={t('apiKey')}
          value={entry.key}
          onValue={(key) => onEntry({ key, ...(key ? { removed: false } : {}) })}
          removed={entry.removed}
          onRemoved={(removed) => onEntry({ removed, ...(removed ? { key: '' } : {}) })}
          configured={configured}
          disabled={saving}
          placeholder={t('keepSaved')}
          removeLabel={t('remove.key')}
          status={
            <>
              {t('key.status')} {configured ? t('configured') : t('notConfigured')}. {t('key.neverShown')} {t('key.required')}
            </>
          }
        />
      ) : null}
    </>
  );
}

type RowActionsProps = Omit<ProviderRowProps, 'configured' | 'onEntry'>;

/** Test, move up/down and remove buttons of one provider card plus the test feedback; Test needs an endpoint and a usable key. */
function ProviderRowActions({
  t,
  entry,
  index,
  count,
  saving,
  ready,
  testPending,
  tested,
  result,
  isError,
  error,
  onTest,
  onMove,
  onRemove,
}: RowActionsProps) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Button type="button" variant="outline" disabled={testPending || saving || !entry.endpoint || !ready} onClick={onTest}>
        {testPending && tested ? t('testing') : t('test.label')}
      </Button>
      <Button
        type="button"
        variant="outline"
        disabled={saving || index === 0}
        onClick={() => onMove(-1)}
        aria-label={t('move.upAt', { number: index + 1 })}
      >
        {t('move.up')}
      </Button>
      <Button
        type="button"
        variant="outline"
        disabled={saving || index === count - 1}
        onClick={() => onMove(1)}
        aria-label={t('move.downAt', { number: index + 1 })}
      >
        {t('move.down')}
      </Button>
      <Button type="button" variant="outline" disabled={saving} onClick={onRemove} aria-label={t('remove.at', { number: index + 1 })}>
        {t('remove.provider')}
      </Button>
      <TestFeedback t={t} tested={tested} result={result} isError={isError} error={error} />
    </div>
  );
}

/**
 * One ordered web-search provider card: priority badge, provider/endpoint inputs, write-only key, test and reorder/remove buttons.
 * `tested` marks the row whose test result is currently displayed; all edits go through the `on*` callbacks.
 */
function ProviderRow(props: ProviderRowProps) {
  return (
    <div className="flex flex-col gap-3 rounded-lg border p-4">
      <ProviderRowFields {...props} />
      <ProviderRowActions {...props} />
    </div>
  );
}

/**
 * Loads web-search settings and owns the edit session: the unsaved draft (frozen at the revision seen when editing began),
 * which row's test result is shown, and the save/test mutations. A successful save clears the draft and refreshes `ai-settings`.
 */
function useWebSearchEditor() {
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [testedIndex, setTestedIndex] = useState<number | null>(null);
  const query = useQuery({ queryKey: ['web-search-settings'], queryFn: () => apiRequest<WebSearch>('/api/v1/settings/web-search') });
  const saved = query.data;
  // Keep the revision observed at edit start even if the query refreshes in the background.
  const view =
    draft ??
    (saved
      ? {
          configuration_revision: saved.configuration_revision,
          providers: saved.providers.map((entry) => ({ ...entry, key: '', removed: false })),
          allow_remote_web_search: saved.allow_remote_web_search,
        }
      : null);
  const save = useMutation({
    mutationFn: (value: Draft) =>
      apiRequest<WebSearch>('/api/v1/settings/web-search', {
        method: 'PUT',
        headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_revision: value.configuration_revision,
          providers: value.providers.map((entry) => ({
            provider: entry.provider,
            endpoint: entry.endpoint || null,
            credential_action: entry.key ? 'replaced' : entry.removed ? 'removed' : 'unchanged',
            api_key: entry.key || null,
          })),
          allow_remote_web_search: value.allow_remote_web_search,
        }),
      }),
    onSuccess: (value) => {
      setDraft(null);
      setTestedIndex(null);
      test.reset();
      client.setQueryData(['web-search-settings'], value);
      void client.invalidateQueries({ queryKey: ['ai-settings'] });
    },
  });
  const test = useMutation({
    mutationFn: (entry: DraftEntry) =>
      apiRequest<TestResult>('/api/v1/settings/web-search/test', {
        method: 'POST',
        headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: entry.provider, endpoint: entry.endpoint || null, api_key: entry.key }),
      }),
  });
  return { query, saved, view, draft, setDraft, testedIndex, setTestedIndex, save, test };
}

/** Cancel/Save buttons: both need unsaved edits, Save also needs every provider to have an endpoint and a usable key. */
function EditorActions({
  t,
  saving,
  dirty,
  valid,
  onCancel,
}: {
  t: ReturnType<typeof useTranslations<'webSearch'>>;
  saving: boolean;
  dirty: boolean;
  valid: boolean;
  onCancel: () => void;
}) {
  return (
    <SettingsActions>
      <Button type="button" variant="outline" disabled={saving || !dirty} onClick={onCancel}>
        {t('cancelChanges')}
      </Button>
      <Button type="submit" disabled={saving || !dirty || !valid}>
        {t('saveButton')}
      </Button>
    </SettingsActions>
  );
}

/** Add-fallback button (max 4 providers), remote-search consent checkbox (needs at least one provider) and environment note. Every edit replaces the draft through `change`. */
function ProviderListFooter({
  t,
  view,
  saving,
  change,
}: {
  t: ReturnType<typeof useTranslations<'webSearch'>>;
  view: Draft;
  saving: boolean;
  change: (next: Draft) => void;
}) {
  return (
    <>
      <Button
        type="button"
        variant="outline"
        className="self-start"
        disabled={saving || view.providers.length >= 4}
        onClick={() =>
          change({
            ...view,
            providers: [
              ...view.providers,
              { provider: 'tavily', endpoint: null, credential_configured: false, destination_id: null, key: '', removed: false },
            ],
          })
        }
      >
        <Plus className="size-4" aria-hidden="true" />
        {t('addFallback')}
      </Button>
      <CheckboxField>
        <Checkbox
          id="web-search-allow"
          aria-describedby={view.providers.length === 0 ? 'web-search-no-providers' : undefined}
          disabled={saving || view.providers.length === 0}
          checked={view.allow_remote_web_search}
          onCheckedChange={(checked) => change({ ...view, allow_remote_web_search: checked === true })}
        />
        <label htmlFor="web-search-allow">{t('allowRemote')}</label>
      </CheckboxField>
      {view.providers.length > 0 ? <p className="m-0 text-xs text-muted-foreground">{t('envNote')}</p> : null}
    </>
  );
}

/** Error panel shown when the web-search settings cannot be loaded; offers a retry. */
function LoadFailed({ t, error, onRetry }: { t: ReturnType<typeof useTranslations<'webSearch'>>; error: unknown; onRetry: () => void }) {
  return (
    <section className="rounded-lg border bg-card p-6">
      <h1>{t('unavailable')}</h1>
      <p className="error">{t(apiFailureKey(error) ?? 'loadFailed')}</p>
      <Button variant="outline" onClick={onRetry}>
        {t('retry')}
      </Button>
    </section>
  );
}

/** Edit ordered web-search providers and their write-only credentials under one remote-search consent. */
export function WebSearchSettingsWorkspace() {
  const t = useTranslations('webSearch');
  const { query, saved, view, draft, setDraft, testedIndex, setTestedIndex, save, test } = useWebSearchEditor();
  if (query.isPending) return <section className="rounded-lg border bg-card p-6 skeleton" aria-label={t('loading')} />;
  if (query.isError || !saved || !view) return <LoadFailed t={t} error={query.error} onRetry={() => query.refetch()} />;
  /** Resolve saved credential status by endpoint so reordering does not move secrets between targets. */
  const configured = (entry: DraftEntry) => saved.providers.some((item) => item.endpoint === entry.endpoint && item.credential_configured);
  /** Check whether this draft can use a new or retained credential, or is keyless. */
  const keyReady = (entry: DraftEntry) => entry.provider === 'searxng' || Boolean(entry.key) || (configured(entry) && !entry.removed);
  const valid = view.providers.every((entry) => Boolean(entry.endpoint) && keyReady(entry));
  /** Replace the form draft and discard test results for the previous configuration. */
  const change = (next: Draft) => {
    setDraft(next);
    setTestedIndex(null);
    test.reset();
  };
  /** Update one provider while keeping the other ordered entries intact. */
  const changeEntry = (index: number, patch: Partial<DraftEntry>) =>
    change({ ...view, providers: view.providers.map((entry, i) => (i === index ? { ...entry, ...patch } : entry)) });
  /** Swap adjacent providers together with their unsaved secret state. */
  const move = (index: number, offset: number) => {
    const providers = [...view.providers];
    [providers[index], providers[index + offset]] = [providers[index + offset], providers[index]];
    change({ ...view, providers });
  };
  return (
    <div className="flex flex-col gap-5">
      <SettingsPageHeader
        eyebrow={t('eyebrow')}
        title={t('title')}
        description={t('description')}
        status={
          <p className="m-0 text-sm text-muted-foreground" role="status">
            {save.isPending ? t('saving') : draft ? t('unsaved') : save.isSuccess ? t('saved') : ''}
          </p>
        }
      />
      <form
        className="flex flex-col gap-5"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate(view);
        }}
      >
        <SettingsSection title={t('provider')}>
          {view.providers.length > 1 ? <p className="m-0 text-sm text-muted-foreground">{t('fallbackNote')}</p> : null}
          {view.providers.length === 0 ? (
            <p id="web-search-no-providers" className="m-0 text-sm text-muted-foreground">
              {t('noProviders')}
            </p>
          ) : null}
          {view.providers.map((entry, index) => (
            <ProviderRow
              key={index}
              t={t}
              entry={entry}
              index={index}
              count={view.providers.length}
              saving={save.isPending}
              configured={configured(entry)}
              ready={keyReady(entry)}
              testPending={test.isPending}
              tested={testedIndex === index}
              result={test.data}
              isError={test.isError}
              error={test.error}
              onEntry={(patch) => changeEntry(index, patch)}
              onTest={() => {
                setTestedIndex(index);
                test.mutate(entry);
              }}
              onMove={(offset) => move(index, offset)}
              onRemove={() => change({ ...view, providers: view.providers.filter((_, i) => i !== index) })}
            />
          ))}
          <ProviderListFooter t={t} view={view} saving={save.isPending} change={change} />
        </SettingsSection>
        {save.isError ? (
          <p className="error" role="alert">
            {t(apiFailureKey(save.error) ?? 'saveFailed')}
          </p>
        ) : null}
        <EditorActions
          t={t}
          saving={save.isPending}
          dirty={Boolean(draft)}
          valid={valid}
          onCancel={() => {
            setDraft(null);
            setTestedIndex(null);
            save.reset();
            test.reset();
          }}
        />
      </form>
    </div>
  );
}
