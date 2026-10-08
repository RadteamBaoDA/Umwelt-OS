'use client';

import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { apiRequest } from '@/core/api';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';

type SystemHealth = { overall: string; components: Record<string, { status: string; connectivity?: string }> };
const labels: Record<string, string> = { postgres: 'Database', redis: 'Background queue', worker: 'Worker', chat_worker: 'Chat worker', model_gateway: 'OmniRoute', graph: 'Knowledge graph', n8n: 'Connector workflows', browser: 'Browser collection' };

/** Fetches and displays health for core services and configured integrations, with retry on request failure. */
function SystemStatus() {
  const health = useQuery({ queryKey: ['system-health'], queryFn: () => apiRequest<SystemHealth>('/api/v1/system/health') });
  if (health.isPending) return <div className="content-panel skeleton" aria-label="Loading system status" />;
  if (health.isError) return <section className="content-panel"><h1>System status is unavailable</h1><p className="muted">Reload after checking the local API and database.</p><Button className="secondary" onClick={() => health.refetch()}>Retry</Button></section>;
  return <section className="content-panel"><span className="brand">Your local workspace</span><h1>System status</h1><p className="muted">Core services are checked live. Optional integrations will appear here when configured.</p><div className="status-grid">{Object.entries(health.data.components).map(([key, component]) => <article className="status-item" key={key}><div className="status-label">{labels[key] ?? key}</div><div className="status-value">{key === 'model_gateway' && component.status === 'unconfigured' ? 'OmniRoute is not configured' : component.status.replaceAll('_', ' ')}{component.connectivity === 'not_tested' && component.status === 'configured' ? ' · connectivity not tested' : ''}</div></article>)}</div><p className="muted" role="status">Overall: {health.data.overall}.</p></section>;
}

/** Renders system status within the workspace shell. */
export default function SystemPage() { return <WorkspaceShell><SystemStatus /></WorkspaceShell>; }
