'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { apiRequest } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useWorkspace } from '@/core/workspace-context';
import { useTranslations } from 'next-intl';
import { SettingsPageHeader, SettingsSection } from '@/modules/settings/settings-layout';
import { ProviderReviewTable, type PendingReview } from '@/modules/settings/provider-review';
import { StatusRow } from '@/modules/observability/status-row';

type SafeConfiguration = {
  multi_workspace_enabled: boolean;
  secure_cookies: boolean;
  google_login_configured: boolean;
  github_app_configured: boolean;
  n8n_configured: boolean;
  mcp_widgets_enabled: boolean;
};
/** Shows a pointer to System status, the account's workspaces, and operator terms-review requests. */
function GlobalSettingsContent() {
  const t = useTranslations('globalSettings');
  const session = useWorkspaceSession();
  const { workspaces, selectWorkspace } = useWorkspace();
  const router = useRouter();
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

  const liveReviews = useQuery({
    queryKey: ['operator-live-map-terms-review'],
    enabled: session.isInstanceAdmin,
    queryFn: ({ signal }) => apiRequest<PendingReview[]>('/api/v1/operator/live-map/terms-review', { signal }),
  });

  if (!session.isInstanceAdmin) return <p role="alert" className="text-muted-foreground">{t('accessDenied')}</p>;
  if (configuration.isPending || reviews.isPending || liveReviews.isPending) return <div className="content-panel skeleton" aria-label={t('loading')} />;
  if (configuration.isError || reviews.isError || liveReviews.isError) return <section className="content-panel">
    <p role="alert" className="text-destructive">{t('loadFailed')}</p>
    <Button variant="outline" onClick={() => { void configuration.refetch(); void reviews.refetch(); void liveReviews.refetch(); }}>{t('retry')}</Button>
  </section>;

  /** Switches the selected workspace and opens its read-only or owner dashboard. */
  const openWorkspace = (id: string) => {
    selectWorkspace(id);
    router.push('/dashboard');
  };

  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('title')} title={t('title')} description={t('description')} />
    <SettingsSection title={t('providerReviewsPending', { count: reviews.data.length + liveReviews.data.length })}>
      {reviews.data.length === 0 && liveReviews.data.length === 0 ? <p className="m-0 text-sm text-muted-foreground">{t('noProviderReviews')}</p> : <ProviderReviewTable rows={[...reviews.data, ...liveReviews.data]} />}
    </SettingsSection>
    <SettingsSection title={t('deployment')}>
      <div className="flex flex-col gap-2">
        <Link className="self-start text-sm underline underline-offset-4" href="/settings/system">{t('viewServices')}</Link>
        <ul className="m-0 list-none p-0">
          {([['multiWorkspace', 'multi_workspace_enabled'], ['secureCookies', 'secure_cookies'], ['googleLogin', 'google_login_configured'], ['githubApp', 'github_app_configured'], ['n8n', 'n8n_configured'], ['mcpWidgets', 'mcp_widgets_enabled']] as const).map(([label, key]) => {
            const on = configuration.data[key];
            return <StatusRow key={key} tone={on ? 'success' : 'muted'} name={t(label)} status={key.endsWith('_configured') ? (on ? t('st_configured') : t('st_not_configured')) : (on ? t('enabled') : t('disabled'))} />;
          })}
        </ul>
      </div>
    </SettingsSection>
    <SettingsSection title={t('workspaces')}>
      <ul className="m-0 flex list-none flex-col gap-3 p-0">
        {workspaces.map((workspace) => <li key={workspace.id} className="flex flex-wrap items-center gap-3 border-b border-border pb-3 last:border-0 last:pb-0">
          <p className="m-0 min-w-0 font-medium">{workspace.name}</p>
          <Button variant="outline" onClick={() => openWorkspace(workspace.id)}>{t('openWorkspace')}</Button>
        </li>)}
      </ul>
    </SettingsSection>
  </div>;
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
  return <GlobalSettingsPage />;
}
