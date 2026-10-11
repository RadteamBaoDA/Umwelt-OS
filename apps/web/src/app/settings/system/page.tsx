'use client';

import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { apiRequest } from '@/core/api';
import { useTranslations } from 'next-intl';
import { SettingsPageHeader, SettingsSection } from '@/modules/settings/settings-layout';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';

type SystemHealth = { overall: string; components: Record<string, { status: string; connectivity?: string }> };
const labels: Record<string, string> = { postgres: 'Database', redis: 'Background queue', worker: 'Worker', chat_worker: 'Chat worker', model_gateway: 'Model gateway', graph: 'Knowledge graph', n8n: 'Connector workflows', browser: 'Browser collection' };

/** Fetches and displays health for core services and configured integrations, with retry on request failure. */
function SystemStatus() {
  const tShell = useTranslations('shell');
  const health = useQuery({ queryKey: ['system-health'], queryFn: () => apiRequest<SystemHealth>('/api/v1/system/health') });
  if (health.isPending) return <div className="content-panel skeleton" aria-label="Loading system status" />;
  if (health.isError) return <section className="content-panel"><h1>System status is unavailable</h1><p className="muted">Reload after checking the local API and database.</p><Button className="secondary" onClick={() => health.refetch()}>Retry</Button></section>;
  return <div className="flex flex-col gap-5"><SettingsPageHeader eyebrow={tShell('settings')} title="System status" description="Core services are checked live. Optional integrations will appear here when configured." /><SettingsSection title="Services"><div className="status-grid mt-0">{Object.entries(health.data.components).map(([key, component]) => <article className="status-item" key={key}><div className="status-label">{labels[key] ?? key}</div><div className="status-value">{key === 'model_gateway' && component.status === 'unconfigured' ? 'Model gateway is not configured' : component.status.replaceAll('_', ' ')}{component.connectivity === 'not_tested' && component.status === 'configured' ? ' · connectivity not tested' : ''}</div></article>)}</div><p className="muted m-0" role="status">Overall: {health.data.overall}.</p></SettingsSection></div>;
}

/** Renders system status within the workspace shell. */
export default function SystemPage() { return <WorkspaceShell><SystemStatus /></WorkspaceShell>; }
