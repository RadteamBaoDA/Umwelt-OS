'use client';

import { TablePagination, usePagination } from '@/components/ui/pagination';
import { useMutation, useQuery, useQueryClient, type UseMutationResult } from '@tanstack/react-query';
import Link from 'next/link';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { StatusDot } from '@/modules/observability/public-api';
import type { Tone } from '@/modules/observability';
import { deactivateConnector, triggerCollection } from '@/modules/sources';
import { SecretField, SettingsActions, SettingsPageHeader, SettingsSection } from '@/components/settings-layout';

type Source = 'database' | 'environment' | 'none';
type N8n = {
  enabled: boolean;
  callback_base_url: string;
  configuration_revision: number;
  service_url: string | null;
  api_key_configured: boolean;
  webhook_token_configured: boolean;
  service_url_source: Source;
  api_key_source: Source;
  webhook_token_source: Source;
};
type TestResult = {
  ok: boolean;
  error: 'credential_required' | 'unreachable' | 'unauthorized' | 'http_error' | null;
  detail: number | null;
};
type Workflow = {
  source_id: string;
  source_name: string;
  source_type: string;
  provider: string | null;
  workflow_id: string | null;
  state: string;
  error_code: string | null;
  transition_phase: string | null;
  last_success_at: string | null;
  last_error_at: string | null;
  last_error_code: string | null;
  collected_at: string | null;
  next_due_at: string | null;
};
type Secret = { value: string; removed: boolean };
const empty: Secret = { value: '', removed: false };
/** Provisioning states the backend can report (ck_connector_provisioning_state). */
const STATES = new Set(['queued', 'provisioning', 'active', 'saved_not_active', 'reconciliation_required', 'disabled']);
const action = (secret: Secret) => (secret.value ? 'replaced' : secret.removed ? 'removed' : 'unchanged');

/** True when the latest run failed: an error is recorded and no success came after it (ISO timestamps compare lexically). */
const failing = (row: Workflow) => row.last_error_at !== null && (row.last_success_at === null || row.last_error_at > row.last_success_at);

/** Status-dot tone for a workflow row; a failing last run outranks the provisioning state. */
function workflowTone(row: Workflow): Tone {
  if (failing(row)) return 'danger';
  if (row.state === 'active') return 'success';
  if (row.state === 'reconciliation_required') return 'warning';
  return 'muted';
}

/** Strips scheme and trailing slash so the header can name the n8n server compactly. */
const hostOf = (url: string) => url.replace(/^https?:\/\//, '').replace(/\/$/, '');

/**
 * One workflow table row: source link, provisioning state, last run and the collect/deactivate actions.
 *
 * @param props - The workflow, whether actions are disabled, the already-translated state label and the action callback.
 * @returns A table row.
 */
function WorkflowRow({
  row,
  disabled,
  stateLabel,
  onAct,
}: {
  row: Workflow;
  disabled: boolean;
  stateLabel: string;
  onAct: (kind: 'deactivate' | 'collect') => void;
}) {
  const t = useTranslations('n8n');
  const display = useDisplayPreferences();
  return (
    <TableRow>
      <TableCell className="py-3">
        <Link
          className="font-medium text-foreground no-underline hover:underline"
          href={`/settings/sources?source=${encodeURIComponent(row.source_id)}`}
        >
          {row.source_name}
        </Link>
        <div className="text-xs text-muted-foreground">
          {row.source_type}
          {row.provider ? ` · ${row.provider}` : ''}
        </div>
      </TableCell>
      <TableCell>
        <span className="inline-flex items-center gap-2 whitespace-nowrap text-sm">
          <StatusDot tone={workflowTone(row)} />
          {stateLabel}
        </span>
      </TableCell>
      <TableCell>
        <div className="tabular-nums">
          {row.last_success_at ? (
            formatDateTime(row.last_success_at, display.locale, display.timezone)
          ) : (
            <span className="text-muted-foreground">{t('never')}</span>
          )}
        </div>
        {/* An older error that a later success superseded stays visible but grey; only a current failure is red. */}
        {row.last_error_code ? (
          <div className={failing(row) ? 'text-xs text-[var(--danger)]' : 'text-xs text-muted-foreground'}>
            {t('lastError', { code: row.last_error_code })}
          </div>
        ) : null}
      </TableCell>
      <TableCell>
        <div className="flex justify-end gap-1">
          <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => onAct('collect')}>
            {t('collectNow')}
          </Button>
          <Button type="button" size="sm" variant="ghost" disabled={disabled} onClick={() => onAct('deactivate')}>
            {t('deactivate')}
          </Button>
        </div>
      </TableCell>
    </TableRow>
  );
}

/** Badge naming where a setting's value comes from (database, environment or unset). */
function SourceBadge({ value }: { value: Source }) {
  const t = useTranslations('n8n');
  return <Badge variant="outline">{t(`source_${value}` as 'source_none')}</Badge>;
}

/** Write-only hint: the source badge beside each label already says whether a value is set, so only explain when one is saved. */
const secretStatus = (t: (key: 'neverShown') => string, configured: boolean) => (configured ? t('neverShown') : '');

/**
 * Integration section: enable switch, service URL, API key and the connection test with its result.
 *
 * @param props - Saved settings, draft values with their setters and the test mutation.
 * @returns The settings section.
 */
function IntegrationSection({
  saved,
  saving,
  enabled,
  onEnabled,
  url,
  onUrl,
  apiKey,
  onApiKey,
  test,
}: {
  saved: N8n;
  saving: boolean;
  enabled: boolean;
  onEnabled: (value: boolean) => void;
  url: string;
  onUrl: (value: string) => void;
  apiKey: Secret;
  onApiKey: (value: Secret) => void;
  test: UseMutationResult<TestResult, Error, void>;
}) {
  const t = useTranslations('n8n');
  const result = test.data;
  return (
    <SettingsSection title={t('integration')} description={t('integrationHelp')}>
      <label className="flex items-center gap-3">
        <Switch checked={enabled} disabled={saving} onCheckedChange={onEnabled} aria-label={t('enabledLabel')} />
        <span className="text-sm font-medium">{t('enabledLabel')}</span>
      </label>
      <label className="field" htmlFor="n8n-service-url">
        <span className="label inline-flex items-center gap-2">
          {t('serviceUrl')}
          <SourceBadge value={saved.service_url_source} />
        </span>
        <Input
          id="n8n-service-url"
          aria-label={t('serviceUrl')}
          type="url"
          disabled={saving}
          value={url}
          onChange={(event) => onUrl(event.target.value)}
          placeholder={t('serviceUrlHelp')}
        />
      </label>
      <div className="flex flex-col gap-2">
        {/* Visible label row carries the source badge; SecretField keeps its own label for the accessible name. */}
        <span className="label inline-flex items-center gap-2" aria-hidden="true">
          {t('apiKey')}
          <SourceBadge value={saved.api_key_source} />
        </span>
        <SecretField
          hideLabel
          id="n8n-api-key"
          label={t('apiKey')}
          value={apiKey.value}
          onValue={(value) => onApiKey({ value, removed: value ? false : apiKey.removed })}
          removed={apiKey.removed}
          onRemoved={(removed) => onApiKey({ value: removed ? '' : apiKey.value, removed })}
          configured={saved.api_key_configured}
          disabled={saving}
          placeholder={saved.api_key_configured ? t('keepSaved') : t('enterValue')}
          removeLabel={t('removeApiKey')}
          status={secretStatus(t, saved.api_key_configured)}
        />
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" variant="outline" disabled={test.isPending} onClick={() => test.mutate()}>
          {test.isPending ? t('testing') : t('test')}
        </Button>
        {result ? (
          <p className="m-0 inline-flex items-center gap-2 text-sm" role="status">
            <StatusDot tone={result.ok ? 'success' : 'danger'} />
            <span className={result.ok ? undefined : 'error'}>
              {result.ok ? t('testOk') : t(`testError_${result.error ?? 'unknown'}` as 'testError_unknown', { code: result.detail ?? '' })}
            </span>
          </p>
        ) : null}
        {test.isError ? (
          <p className="error m-0" role="alert">
            {t(apiFailureKey(test.error) ?? 'requestFailed')}
          </p>
        ) : null}
      </div>
    </SettingsSection>
  );
}

/**
 * Callback base URL section with a clipboard copy button and its outcome.
 *
 * @param props - The URL, the copy outcome and the copy handler.
 * @returns The settings section.
 */
function ConnectionSection({ value, copied, onCopy }: { value: string; copied: 'idle' | 'ok' | 'failed'; onCopy: () => void }) {
  const t = useTranslations('n8n');
  return (
    <SettingsSection title={t('connectionTitle')} description={t('collectorNote')}>
      <div className="flex flex-wrap items-center gap-3">
        {/* Joined read-only field and Copy share one border with squared inner corners. */}
        <div className="flex min-w-0 max-w-xl flex-1">
          <Input readOnly aria-label={t('callbackBaseUrl')} value={value} className="rounded-r-none" />
          <Button type="button" variant="outline" className="-ml-px rounded-l-none" onClick={onCopy}>
            {t('copy')}
          </Button>
        </div>
        {copied === 'idle' ? null : (
          <span className="text-sm text-muted-foreground" role="status">
            {copied === 'ok' ? t('copied') : t('copyFailed')}
          </span>
        )}
      </div>
    </SettingsSection>
  );
}

/**
 * Write-only webhook token section.
 *
 * @param props - Saved settings, the draft token and its setter.
 * @returns The settings section.
 */
function WebhookTokenSection({
  saved,
  saving,
  token,
  onToken,
}: {
  saved: N8n;
  saving: boolean;
  token: Secret;
  onToken: (value: Secret) => void;
}) {
  const t = useTranslations('n8n');
  return (
    <SettingsSection
      title={
        <span className="inline-flex flex-wrap items-center gap-2">
          {t('webhookToken')}
          <SourceBadge value={saved.webhook_token_source} />
        </span>
      }
      description={t('tokenWarning')}
    >
      <SecretField
        hideLabel
        id="n8n-webhook-token"
        label={t('webhookToken')}
        value={token.value}
        onValue={(value) => onToken({ value, removed: value ? false : token.removed })}
        removed={token.removed}
        onRemoved={(removed) => onToken({ value: removed ? '' : token.value, removed })}
        configured={saved.webhook_token_configured}
        disabled={saving}
        placeholder={saved.webhook_token_configured ? t('keepSaved') : t('enterValue')}
        removeLabel={t('removeWebhookToken')}
        status={secretStatus(t, saved.webhook_token_configured)}
      />
    </SettingsSection>
  );
}

/**
 * The screen's first block: every n8n-provisioned source with its state, last run and the collect-now job.
 * Row actions are disabled while the integration is off; the source name links to its editor.
 */
function WorkflowsSection({ enabled }: { enabled: boolean }) {
  const t = useTranslations('n8n');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['n8n-workflows'], queryFn: () => apiRequest<Workflow[]>('/api/v1/connectors/n8n/workflows') });
  const act = useMutation({
    mutationFn: async ({ id, kind }: { id: string; kind: 'deactivate' | 'collect' }): Promise<unknown> =>
      kind === 'deactivate' ? deactivateConnector(id, csrfToken) : triggerCollection(id, csrfToken),
    // Refetch even on failure so the table reflects whatever state the backend actually reached.
    onSettled: () => client.invalidateQueries({ queryKey: ['n8n-workflows'] }),
  });
  const rows = query.data;
  const pager = usePagination(rows ?? []);
  /** Translates a backend provisioning state, falling back to the raw value for unknown states. */
  const stateLabel = (state: string) => (STATES.has(state) ? t(`state_${state}` as 'state_active') : state);
  const failed = rows?.filter(failing).length ?? 0;
  return (
    <section aria-labelledby="workflows-list" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="workflows-list" className="m-0 text-base font-semibold">
          {t('workflows')}
        </h2>
        {rows && rows.length > 0 ? (
          <p className="m-0 text-sm text-muted-foreground tabular-nums">
            {t('summaryTotal', { count: rows.length })} ·{' '}
            {t('summaryActive', { count: rows.filter((row) => row.state === 'active').length })}
            {failed ? (
              <>
                {' '}
                · <span className="text-[var(--danger)]">{t('summaryFailing', { count: failed })}</span>
              </>
            ) : null}
          </p>
        ) : null}
      </div>
      {query.isPending ? (
        <div className="skeleton h-16" role="status" aria-label={t('workflowsLoading')} />
      ) : query.isError || !rows ? (
        <div className="flex flex-wrap items-center gap-3">
          <p className="error m-0" role="alert">
            {t(apiFailureKey(query.error) ?? 'workflowsLoadFailed')}
          </p>
          <Button type="button" variant="outline" onClick={() => query.refetch()}>
            {t('retry')}
          </Button>
        </div>
      ) : rows.length === 0 ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-muted px-4 py-5">
          <div>
            <p className="m-0 font-medium">{t('workflowsEmpty')}</p>
            <p className="m-0 text-sm text-muted-foreground">{t('workflowsEmptyHint')}</p>
          </div>
          <Button asChild variant="outline">
            <Link href="/settings/sources">{t('addSource')}</Link>
          </Button>
        </div>
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('colName')}</TableHead>
                <TableHead>{t('colState')}</TableHead>
                <TableHead>{t('colLastRun')}</TableHead>
                <TableHead>
                  <span className="sr-only">{t('colActions')}</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pager.pageItems.map((row) => (
                <WorkflowRow
                  key={row.source_id}
                  row={row}
                  disabled={!enabled || act.isPending}
                  stateLabel={stateLabel(row.state)}
                  onAct={(kind) => act.mutate({ id: row.source_id, kind })}
                />
              ))}
            </TableBody>
          </Table>
          <TablePagination {...pager} />
        </>
      )}
      {act.isError ? (
        <p className="error m-0" role="alert">
          {t(apiFailureKey(act.error) ?? 'actionFailed')}
        </p>
      ) : null}
    </section>
  );
}

/**
 * Loads the saved n8n settings and owns the unsaved draft (URL, enable switch, write-only secrets) plus the
 * save and connection-test mutations. A successful save clears the draft and the stale test result and
 * replaces the cached settings with the server's new revision.
 *
 * @returns The settings query, draft values with setters, derived current values and the mutations.
 */
function useN8nDraft() {
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [url, setUrl] = useState<string | null>(null);
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [apiKey, setApiKey] = useState<Secret>(empty);
  const [token, setToken] = useState<Secret>(empty);
  const query = useQuery({ queryKey: ['n8n-settings'], queryFn: () => apiRequest<N8n>('/api/v1/settings/n8n') });
  const saved = query.data;
  const currentUrl = url ?? saved?.service_url ?? '';
  const currentEnabled = enabled ?? saved?.enabled ?? false;
  const reset = () => {
    setUrl(null);
    setEnabled(null);
    setApiKey(empty);
    setToken(empty);
  };
  const headers = { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' };
  const save = useMutation({
    mutationFn: (value: N8n) =>
      apiRequest<N8n>('/api/v1/settings/n8n', {
        method: 'PUT',
        headers,
        body: JSON.stringify({
          expected_revision: value.configuration_revision,
          enabled: currentEnabled,
          service_url: currentUrl.trim() || null,
          api_key_action: action(apiKey),
          api_key: apiKey.value || null,
          webhook_token_action: action(token),
          webhook_token: token.value || null,
        }),
      }),
    onSuccess: (value) => {
      reset();
      test.reset();
      client.setQueryData(['n8n-settings'], value);
    },
  });
  const test = useMutation({
    mutationFn: () =>
      apiRequest<TestResult>('/api/v1/settings/n8n/test', {
        method: 'POST',
        headers,
        body: JSON.stringify({ service_url: currentUrl.trim() || null, api_key: apiKey.value }),
      }),
  });
  return {
    query,
    saved,
    url,
    setUrl,
    enabled,
    setEnabled,
    apiKey,
    setApiKey,
    token,
    setToken,
    currentUrl,
    currentEnabled,
    reset,
    save,
    test,
  };
}

/**
 * Save-state line with the Cancel and Save buttons of the n8n server form.
 *
 * @param props - Whether a save is running, whether the draft differs from saved, whether the last save succeeded and the cancel handler.
 * @returns The form action bar.
 */
function FormActions({ saving, dirty, saved, onCancel }: { saving: boolean; dirty: boolean; saved: boolean; onCancel: () => void }) {
  const t = useTranslations('n8n');
  return (
    <SettingsActions>
      <p className="m-0 mr-auto self-center text-sm text-muted-foreground" role="status">
        {saving ? t('saving') : dirty ? t('unsaved') : saved ? t('saved') : ''}
      </p>
      <Button type="button" variant="outline" disabled={saving || !dirty} onClick={onCancel}>
        {t('cancelChanges')}
      </Button>
      <Button type="submit" disabled={saving || !dirty}>
        {t('saveButton')}
      </Button>
    </SettingsActions>
  );
}

/**
 * Unavailable state shown when the n8n settings cannot be loaded.
 *
 * @param props - The query error (mapped to a message key) and the retry handler.
 * @returns Error card with a retry button.
 */
function LoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const t = useTranslations('n8n');
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

/**
 * Workflows screen: the provisioned workflow list first, then the n8n server form
 * (enable switch, service URL, API key with a connection test, callback URL and webhook token).
 * The header states whether n8n is on and which server it points at; secrets stay write-only.
 */
// eslint-disable-next-line complexity -- stateful workflow whose branches mirror independent UI/request states (loading, error, conflict, per-step content); extraction into per-state components is tracked as follow-up R3.6 so this refactor stays behaviour-neutral
export function N8nSettingsWorkspace() {
  const t = useTranslations('n8n');
  const {
    query,
    saved,
    url,
    setUrl,
    enabled,
    setEnabled,
    apiKey,
    setApiKey,
    token,
    setToken,
    currentUrl,
    currentEnabled,
    reset,
    save,
    test,
  } = useN8nDraft();
  const [copied, setCopied] = useState<'idle' | 'ok' | 'failed'>('idle');
  if (query.isPending) return <section className="rounded-lg border bg-card p-6 skeleton" aria-label={t('loading')} />;
  if (query.isError || !saved) return <LoadError error={query.error} onRetry={() => query.refetch()} />;
  const dirty =
    (enabled !== null && enabled !== saved.enabled) ||
    (url !== null && url !== (saved.service_url ?? '')) ||
    action(apiKey) !== 'unchanged' ||
    action(token) !== 'unchanged';
  /** Copies the callback base URL; clipboard may be unavailable or denied, so failure is surfaced instead of thrown. */
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(saved.callback_base_url);
      setCopied('ok');
    } catch {
      setCopied('failed');
    }
  };
  return (
    <div className="flex flex-col gap-8">
      {/* Header status reflects the saved server state, not unsaved edits in the form below. */}
      <SettingsPageHeader
        title={t('title')}
        description={t('description')}
        status={
          <p className="m-0 inline-flex items-center gap-2 text-sm" role="status">
            <StatusDot tone={saved.enabled ? 'success' : 'muted'} />
            <span>{saved.enabled ? t('integrationOn') : t('integrationOff')}</span>
            {saved.service_url ? <span className="text-muted-foreground">· {hostOf(saved.service_url)}</span> : null}
          </p>
        }
      />
      {currentEnabled ? null : (
        <p className="m-0 rounded-lg bg-muted px-4 py-3 text-sm" role="note">
          {t('disabledWarning')}
        </p>
      )}
      <WorkflowsSection enabled={saved.enabled} />
      <form
        className="flex flex-col gap-6"
        aria-labelledby="n8n-server"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate(saved);
        }}
      >
        <h2 id="n8n-server" className="m-0 text-base font-semibold">
          {t('serverTitle')}
        </h2>
        <IntegrationSection
          saved={saved}
          saving={save.isPending}
          enabled={currentEnabled}
          onEnabled={setEnabled}
          url={currentUrl}
          onUrl={setUrl}
          apiKey={apiKey}
          onApiKey={setApiKey}
          test={test}
        />
        <ConnectionSection value={saved.callback_base_url} copied={copied} onCopy={copy} />
        <WebhookTokenSection saved={saved} saving={save.isPending} token={token} onToken={setToken} />
        {save.isError ? (
          <p className="error m-0" role="alert">
            {t(apiFailureKey(save.error) ?? 'saveFailed')}
          </p>
        ) : null}
        <FormActions
          saving={save.isPending}
          dirty={dirty}
          saved={save.isSuccess}
          onCancel={() => {
            reset();
            save.reset();
            test.reset();
          }}
        />
      </form>
    </div>
  );
}
