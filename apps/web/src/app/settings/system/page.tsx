'use client';

import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { apiRequest } from '@/core/api';
import { useTranslations } from 'next-intl';
import { SettingsPageHeader, SettingsSection } from '@/modules/settings/settings-layout';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';

type SystemHealth = { overall: string; components: Record<string, { status: string; connectivity?: string }> };
/** Maps backend component ids to `uiSweep` message keys for their display labels. */
const labelKeys: Record<string, string> = { postgres: 'svcPostgres', redis: 'svcRedis', worker: 'svcWorker', chat_worker: 'svcChatWorker', model_gateway: 'svcModelGateway', graph: 'svcGraph', n8n: 'svcN8n', browser: 'svcBrowser' };

/** Fetches and displays health for core services and configured integrations, with retry on request failure. */
function SystemStatus() {
  const tShell = useTranslations('shell');
  const ts = useTranslations('uiSweep');
  const tc = useTranslations('common');
  const health = useQuery({ queryKey: ['system-health'], queryFn: () => apiRequest<SystemHealth>('/api/v1/system/health') });
  if (health.isPending) return <div className="content-panel skeleton" aria-label={ts('systemLoading')} />;
  if (health.isError) return <section className="content-panel"><h1>{ts('systemUnavailable')}</h1><p className="muted">{ts('systemReload')}</p><Button className="secondary" onClick={() => health.refetch()}>{tc('retry')}</Button></section>;
  return <div className="flex flex-col gap-5"><SettingsPageHeader eyebrow={tShell('settings')} title={ts('systemTitle')} description={ts('systemDescription')} /><SettingsSection title={ts('systemServices')}><div className="status-grid mt-0">{Object.entries(health.data.components).map(([key, component]) => <article className="status-item" key={key}><div className="status-label">{labelKeys[key] ? ts(labelKeys[key]) : key}</div><div className="status-value">{key === 'model_gateway' && component.status === 'unconfigured' ? ts('gatewayNotConfigured') : component.status.replaceAll('_', ' ')}{component.connectivity === 'not_tested' && component.status === 'configured' ? ts('connectivityNotTested') : ''}</div></article>)}</div><p className="muted m-0" role="status">{ts('systemOverall', { status: health.data.overall })}</p></SettingsSection></div>;
}

/** Renders system status within the workspace shell. */
export default function SystemPage() { return <WorkspaceShell><SystemStatus /></WorkspaceShell>; }
