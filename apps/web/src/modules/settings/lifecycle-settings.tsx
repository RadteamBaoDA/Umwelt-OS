'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { apiRequest } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';

type Retention = { configuration_revision: number; agent_trace_days: number; raw_source_retention: 'retain'; document_history_retention: 'retain' };
type Module = { id: string; name: string; enabled: boolean; explicitly_disabled: boolean; blocked_by: string[] };
type ModuleState = { configuration_revision: number; modules: Module[] };
type Summary = { completed_at: string | null; agent_traces_redacted: number; temporary_data_deleted: number; next_eligible_at: string | null };

/** Renders revision-fenced retention controls, dependency-aware module switches and the latest cleanup outcome. */
export function LifecycleSettings() {
  const t = useTranslations('observability');
  const locale = useLocale() === 'vi-vi' ? 'vi-VN' : 'en-US';
  const { csrfToken } = useWorkspaceSession();
  const cache = useQueryClient();
  const retention = useQuery({ queryKey: ['retention-settings'], queryFn: () => apiRequest<Retention>('/api/v1/settings/retention') });
  const modules = useQuery({ queryKey: ['module-lifecycle'], queryFn: () => apiRequest<ModuleState>('/api/v1/settings/modules') });
  const summary = useQuery({ queryKey: ['maintenance-summary'], queryFn: () => apiRequest<Summary>('/api/v1/system/maintenance') });
  const [days, setDays] = useState<number | null>(null);
  const saveRetention = useMutation({
    mutationFn: (value: Retention) => apiRequest<Retention>('/api/v1/settings/retention', { method: 'PATCH', headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: value.configuration_revision, agent_trace_days: days ?? value.agent_trace_days }) }),
    onSuccess: (value) => { setDays(null); cache.setQueryData(['retention-settings'], value); },
  });
  const toggleModule = useMutation({
    mutationFn: ({ state, module }: { state: ModuleState; module: Module }) => apiRequest<ModuleState>('/api/v1/settings/modules', { method: 'PATCH', headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: state.configuration_revision, module_id: module.id, enabled: !module.enabled }) }),
    onSuccess: (value) => { cache.setQueryData(['module-lifecycle'], value); },
  });

  return <section className="mt-6 space-y-5 border-t border-border pt-5" aria-labelledby="lifecycle-title">
    <h2 id="lifecycle-title" className="text-lg font-semibold">{t('lifecycleTitle')}</h2>
    <article className="space-y-3">
      <h3 className="font-medium">{t('retentionTitle')}</h3>
      <p className="muted text-sm">{t('retentionDescription')}</p>
      {retention.data && <div className="flex flex-wrap items-end gap-3">
        <label className="label">{t('agentTraceDays')}<input className="input mt-1 w-32" type="number" min={1} max={3650} value={days ?? retention.data.agent_trace_days} onChange={(event) => setDays(Number(event.target.value))} /></label>
        <Button className="secondary" disabled={saveRetention.isPending || days === null || days < 1 || days > 3650} onClick={() => saveRetention.mutate(retention.data!)}>{t('saveRetention')}</Button>
      </div>}
      {retention.isError && <p role="alert" className="text-destructive">{t('lifecycleLoadFailed')}</p>}
      {saveRetention.isError && <p role="alert" className="text-destructive">{t('lifecycleSaveFailed')}</p>}
    </article>
    <article className="space-y-3">
      <h3 className="font-medium">{t('modulesTitle')}</h3>
      <p className="muted text-sm">{t('modulesDescription')}</p>
      {modules.data?.modules.map((module) => <label key={module.id} className="flex items-center justify-between gap-4 border-b border-border py-2 text-sm">
        <span>{module.name}{module.blocked_by.length > 0 && <span className="muted block">{t('blockedBy')}: {module.blocked_by.join(', ')}</span>}</span>
        <input aria-label={`${module.name}: ${module.enabled ? t('enabled') : t('disabled')}`} type="checkbox" checked={module.enabled} disabled={toggleModule.isPending || module.blocked_by.length > 0} onChange={() => toggleModule.mutate({ state: modules.data!, module })} />
      </label>)}
      {modules.isError && <p role="alert" className="text-destructive">{t('lifecycleLoadFailed')}</p>}
      {toggleModule.isError && <p role="alert" className="text-destructive">{t('moduleSaveFailed')}</p>}
    </article>
    <article className="space-y-2">
      <h3 className="font-medium">{t('maintenanceTitle')}</h3>
      {summary.data && <p className="muted text-sm">{t('maintenanceCounts', { traces: summary.data.agent_traces_redacted, temporary: summary.data.temporary_data_deleted })}{summary.data.completed_at ? ` · ${new Date(summary.data.completed_at).toLocaleString(locale)}` : ''}{summary.data.next_eligible_at ? ` · ${t('nextEligible')}: ${new Date(summary.data.next_eligible_at).toLocaleString(locale)}` : ''}</p>}
      {summary.isError && <p className="muted text-sm">{t('lifecycleLoadFailed')}</p>}
    </article>
  </section>;
}
