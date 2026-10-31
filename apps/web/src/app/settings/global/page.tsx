'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { apiRequest, csrfHeaders } from '@/core/api';
import { safeHttpUrl } from '@/core/safe-url';
import { useDisplayPreferences } from '@/core/query-provider';
import { normalizeFormattingLocale } from '@/core/i18n';
import { connectorKeys, getConnectorCatalog } from '@/modules/sources/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { useWorkspace } from '@/core/workspace-context';
import { useTranslations } from 'next-intl';
import { SettingsPageHeader, SettingsSection } from '@/modules/settings/settings-layout';

type SafeConfiguration = {
  multi_workspace_enabled: boolean;
  secure_cookies: boolean;
  google_login_configured: boolean;
  github_app_configured: boolean;
  n8n_configured: boolean;
  mcp_widgets_enabled: boolean;
};
type PendingReview = {
  workspace_id: string;
  workspace_name: string;
  source_id: string;
  provider_id: string;
  terms_url: string;
  terms_version: string;
  declared_use: string;
  operator_review_state: string;
};
type ReviewDecision = {
  row: PendingReview;
  state: 'approved' | 'rejected';
  allowed_use?: 'personal' | 'noncommercial' | 'commercial';
  evidence_ref: string;
};

/** Shows a pointer to System status, the account's workspaces, and operator terms-review requests. */
function GlobalSettingsContent() {
  const t = useTranslations('globalSettings');
  const session = useWorkspaceSession();
  const { workspaces, selectWorkspace } = useWorkspace();
  const router = useRouter();
  const client = useQueryClient();
  const configuration = useQuery({
    queryKey: ['global-system-configuration'],
    enabled: session.isInstanceAdmin,
    queryFn: ({ signal }) => apiRequest<SafeConfiguration>('/api/v1/system/configuration', { signal }),
  });
  const reviews = useQuery({
    queryKey: ['operator-provider-terms-review'],
    enabled: session.isInstanceAdmin,
    queryFn: ({ signal }) => apiRequest<PendingReview[]>('/api/v1/operator/connectors/terms-review', { signal }),
  });

  if (!session.isInstanceAdmin) return <p role="alert" className="text-muted-foreground">{t('accessDenied')}</p>;
  if (configuration.isPending || reviews.isPending) return <div className="content-panel skeleton" aria-label={t('loading')} />;
  if (configuration.isError || reviews.isError) return <section className="content-panel">
    <p role="alert" className="text-destructive">{t('loadFailed')}</p>
    <Button variant="outline" onClick={() => { void configuration.refetch(); void reviews.refetch(); }}>{t('retry')}</Button>
  </section>;

  /** Switches the selected workspace and opens its read-only or owner dashboard. */
  const openWorkspace = (id: string) => {
    selectWorkspace(id);
    router.push('/dashboard');
  };

  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('title')} title={t('title')} description={t('description')} />
    <SettingsSection title={t('system')}>
      <p className="m-0 text-sm text-muted-foreground">{t('healthOnSystem')} <Link className="underline underline-offset-4 text-foreground" href="/settings/system">{t('system')}</Link></p>
      <div className="border-t border-border pt-4">
        <h3 className="mb-3 text-sm font-semibold">{t('configuration')}</h3>
        <dl className="m-0 grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
          {([['multiWorkspace', 'multi_workspace_enabled'], ['secureCookies', 'secure_cookies'], ['googleLogin', 'google_login_configured'], ['githubApp', 'github_app_configured'], ['n8n', 'n8n_configured'], ['mcpWidgets', 'mcp_widgets_enabled']] as const).map(([label, key]) => <div key={key}>
            <dt className="text-muted-foreground">{t(label)}</dt><dd className="m-0 font-medium">{configuration.data[key] ? t('enabled') : t('disabled')}</dd>
          </div>)}
        </dl>
      </div>
    </SettingsSection>
    <SettingsSection title={t('workspaces')}>
      <ul className="m-0 flex list-none flex-col gap-3 p-0">
        {workspaces.map((workspace) => <li key={workspace.id} className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3 last:border-0 last:pb-0">
          <div className="min-w-0"><p className="m-0 font-medium">{workspace.name}</p></div>
          <Button variant="outline" onClick={() => openWorkspace(workspace.id)}>{t('openWorkspace')}</Button>
        </li>)}
      </ul>
    </SettingsSection>
    <SettingsSection title={t('providerReviews')}>
      {reviews.data.length === 0 ? <p className="m-0 text-sm text-muted-foreground">{t('noProviderReviews')}</p> :
        <div className="flex flex-col gap-4">{reviews.data.map((row) => <ProviderReviewRow key={`${row.workspace_id}-${row.source_id}`} row={row} />)}</div>}
    </SettingsSection>
  </div>;
}

/** Records an operator decision with its exact terms version and required evidence reference. */
function ProviderReviewRow({ row }: { row: PendingReview }) {
  const t = useTranslations('globalSettings');
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const [evidence, setEvidence] = useState('');
  const display = useDisplayPreferences();
  const catalog = useQuery({ queryKey: connectorKeys.catalog, queryFn: ({ signal }) => getConnectorCatalog(signal), staleTime: 5 * 60_000 });
  // Falls back to the raw provider id when the catalog has no entry, so unknown providers stay identifiable.
  const providerName = catalog.data?.find((entry) => entry.provider_id === row.provider_id)?.label ?? row.provider_id;
  // Terms versions are ISO dates (UTC midnight, so no zone shift); any other format is shown verbatim rather than guessed at.
  const termsVersion = /^\d{4}-\d{2}-\d{2}$/.test(row.terms_version)
    ? new Intl.DateTimeFormat(normalizeFormattingLocale(display.locale), { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date(row.terms_version))
    : row.terms_version;
  const useLabel = (value: string) => (value === 'personal' || value === 'noncommercial' || value === 'commercial' ? t(`use.${value}`) : value);
  const [allowedUse, setAllowedUse] = useState<ReviewDecision['allowed_use'] | ''>(
    row.declared_use === 'personal' || row.declared_use === 'noncommercial' || row.declared_use === 'commercial' ? row.declared_use : '',
  );
  const mutation = useMutation({
    mutationFn: ({ state, allowed_use, evidence_ref }: Omit<ReviewDecision, 'row'>) => apiRequest(
      `/api/v1/operator/connectors/${encodeURIComponent(row.workspace_id)}/${encodeURIComponent(row.source_id)}/terms-review`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(session.csrfToken) },
        body: JSON.stringify({ state, ...(allowed_use ? { allowed_use } : {}), evidence_ref, reviewed_terms_version: row.terms_version }),
      },
    ),
    onSuccess: () => client.invalidateQueries({ queryKey: ['operator-provider-terms-review'] }),
  });

  /** Submits one decision only when the required evidence and approval scope are present. */
  const submit = (event: { preventDefault(): void }, state: ReviewDecision['state']) => {
    event.preventDefault();
    const evidence_ref = evidence.trim();
    if (!evidence_ref || (state === 'approved' && !allowedUse)) return;
    mutation.mutate({ state, ...(state === 'approved' && allowedUse ? { allowed_use: allowedUse } : {}), evidence_ref });
  };

  return <article className="flex flex-col gap-3 border-b border-border pb-4 last:border-0 last:pb-0">
    <dl className="m-0 grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
      <div><dt className="text-muted-foreground">{t('workspaces')}</dt><dd className="m-0 font-medium">{row.workspace_name}</dd></div>
      <div><dt className="text-muted-foreground">{t('provider')}</dt><dd className="m-0 flex flex-wrap items-center gap-2 font-medium">{providerName}{safeHttpUrl(row.terms_url) && <a href={safeHttpUrl(row.terms_url)} target="_blank" rel="noopener noreferrer" className="font-normal text-primary underline underline-offset-4">{t('termsLink')}</a>}</dd></div>
      <div><dt className="text-muted-foreground">{t('termsVersion')}</dt><dd className="m-0">{termsVersion}</dd></div>
      <div><dt className="text-muted-foreground">{t('declaredUse')}</dt><dd className="m-0">{useLabel(row.declared_use)}</dd></div>
    </dl>
    <form className="flex flex-col gap-3" onSubmit={(event) => submit(event, 'approved')}>
      <div className="grid gap-2 sm:grid-cols-2">
        <div className="flex flex-col gap-2">
          <Label htmlFor={`evidence-${row.source_id}`}>{t('evidence')}</Label>
          <Input id={`evidence-${row.source_id}`} required value={evidence} onChange={(event) => setEvidence(event.target.value)} aria-describedby={`evidence-help-${row.source_id}`} />
          <p id={`evidence-help-${row.source_id}`} className="m-0 text-xs text-muted-foreground">{t('evidenceHelp')}</p>
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor={`allowed-use-${row.source_id}`}>{t('allowedUse')}</Label>
          <Select value={allowedUse} onValueChange={(value) => setAllowedUse(value as NonNullable<ReviewDecision['allowed_use']>)}>
            <SelectTrigger id={`allowed-use-${row.source_id}`}><SelectValue placeholder={t('allowedUse')} /></SelectTrigger>
            <SelectContent>
              <SelectItem value="personal">{t('use.personal')}</SelectItem>
              <SelectItem value="noncommercial">{t('use.noncommercial')}</SelectItem>
              <SelectItem value="commercial">{t('use.commercial')}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      {mutation.isError && <p role="alert" className="m-0 text-sm text-destructive">{t('reviewFailed')}</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={mutation.isPending || !evidence.trim() || !allowedUse}>
          {mutation.isPending ? t('saving') : t('approve')}
        </Button>
        <Button type="button" variant="outline" disabled={mutation.isPending || !evidence.trim()} onClick={(event) => submit(event, 'rejected')}>
          {mutation.isPending ? t('saving') : t('reject')}
        </Button>
      </div>
    </form>
  </article>;
}

/** Renders global controls only for the instance administrator inside the authenticated shell. */
function GlobalSettingsPage() {
  const t = useTranslations('globalSettings');
  const session = useWorkspaceSession();
  if (!session.isInstanceAdmin) return <p role="alert" className="text-muted-foreground">{t('accessDenied')}</p>;
  return <GlobalSettingsContent />;
}

/** Renders the instance administrator settings route inside the workspace shell. */
export default function GlobalPage() {
  return <WorkspaceShell><GlobalSettingsPage /></WorkspaceShell>;
}
