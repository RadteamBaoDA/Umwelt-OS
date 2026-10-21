'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { apiRequest, csrfHeaders } from '@/core/api';
import { safeHttpUrl } from '@/core/safe-url';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { useWorkspace } from '@/core/workspace-context';
import { useTranslations } from 'next-intl';
import { SettingsPageHeader, SettingsSection } from '@/modules/settings/settings-layout';

type SystemHealth = { overall: string; components: Record<string, { status: string; connectivity?: string }> };
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

/** Shows filtered service health, the account's workspaces, and operator terms-review requests. */
function GlobalSettingsContent() {
  const t = useTranslations('globalSettings');
  const session = useWorkspaceSession();
  const { workspaces, selectWorkspace } = useWorkspace();
  const router = useRouter();
  const client = useQueryClient();
  const health = useQuery({
    queryKey: ['global-system-health'],
    enabled: session.isInstanceAdmin,
    queryFn: ({ signal }) => apiRequest<SystemHealth>('/api/v1/system/health', { signal }),
  });
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
  if (health.isPending || configuration.isPending || reviews.isPending) return <div className="content-panel skeleton" aria-label={t('loading')} />;
  if (health.isError || configuration.isError || reviews.isError) return <section className="content-panel">
    <p role="alert" className="text-destructive">{t('loadFailed')}</p>
    <Button variant="outline" onClick={() => { void health.refetch(); void configuration.refetch(); void reviews.refetch(); }}>{t('retry')}</Button>
  </section>;

  /** Switches the selected workspace and opens its read-only or owner dashboard. */
  const openWorkspace = (id: string) => {
    selectWorkspace(id);
    router.push('/dashboard');
  };

  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('title')} title={t('title')} description={t('description')} />
    <SettingsSection title={t('system')}>
      <div className="overflow-x-auto">
        <table className="w-full min-w-80 text-left text-sm">
          <thead><tr className="border-b border-border text-muted-foreground"><th className="py-2 pr-4 font-medium">{t('service')}</th><th className="py-2 font-medium">{t('status')}</th></tr></thead>
          <tbody>{Object.entries(health.data.components).map(([name, item]) => <tr className="border-b border-border last:border-0" key={name}>
            <th scope="row" className="py-2 pr-4 font-medium">{name}</th><td className="py-2">{item.status.replaceAll('_', ' ')}</td>
          </tr>)}</tbody>
        </table>
      </div>
      <p className="m-0 text-sm text-muted-foreground" role="status">{t('overall', { status: health.data.overall })}</p>
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
          <div className="min-w-0"><p className="m-0 font-medium">{workspace.name}</p><p className="m-0 text-sm text-muted-foreground">{t('owner', { id: workspace.owner_user_id })}</p></div>
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

  return <article className="flex flex-col gap-3 rounded-lg border border-border p-4">
    <dl className="m-0 grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
      <div><dt className="text-muted-foreground">{t('workspaces')}</dt><dd className="m-0 font-medium">{row.workspace_name}</dd></div>
      <div><dt className="text-muted-foreground">{t('provider')}</dt><dd className="m-0 flex flex-wrap items-center gap-2 font-medium">{row.provider_id}{safeHttpUrl(row.terms_url) && <a href={safeHttpUrl(row.terms_url)} target="_blank" rel="noopener noreferrer" className="font-normal text-primary underline underline-offset-4">{t('termsLink')}</a>}</dd></div>
      <div><dt className="text-muted-foreground">{t('termsVersion')}</dt><dd className="m-0">{row.terms_version}</dd></div>
      <div><dt className="text-muted-foreground">{t('declaredUse')}</dt><dd className="m-0">{row.declared_use}</dd></div>
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
              <SelectItem value="personal">personal</SelectItem>
              <SelectItem value="noncommercial">noncommercial</SelectItem>
              <SelectItem value="commercial">commercial</SelectItem>
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
