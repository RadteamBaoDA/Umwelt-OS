'use client';

import * as React from 'react';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useFormatter, useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { apiRequest, csrfHeaders } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { AgentList } from './agent-list';
import { cancelAgentRun, getAgentProfiles, getAgentRun, listAgentRuns, updateAgentProfile, type AgentProfile, type AgentRun } from './api';
import { AgentRunDetail } from './run-detail';

interface SourceItem { id: string; name: string; status: string }
interface SourcePage { items: SourceItem[]; next_cursor: string | null }
interface AISettingsAliases { aliases: Record<string, { model: string; version: string | null; destination: string }> }
const profileTitleKeys: Record<string, 'agentProfileSupervisor' | 'agentProfileKnowledge' | 'agentProfileResearch' | 'agentProfilePersonal' | 'agentProfileProject' | 'agentProfileNews' | 'agentProfilePlanning' | 'agentProfileAutomation'> = {
  supervisor: 'agentProfileSupervisor', knowledge: 'agentProfileKnowledge', research: 'agentProfileResearch',
  personal: 'agentProfilePersonal', project: 'agentProfileProject', news: 'agentProfileNews',
  planning: 'agentProfilePlanning', automation: 'agentProfileAutomation',
};
const runStatusKeys: Record<string, 'agentRunQueued' | 'agentRunRunning' | 'agentRunWaitingApproval' | 'agentRunSucceeded' | 'agentRunFailed' | 'agentRunCancelled'> = {
  queued: 'agentRunQueued', running: 'agentRunRunning', waiting_approval: 'agentRunWaitingApproval',
  succeeded: 'agentRunSucceeded', failed: 'agentRunFailed', cancelled: 'agentRunCancelled',
};

/** Manage owner-authored profiles, exact current tools/source grants, and a bounded recent run window. */
export function AgentSettingsWorkspace() {
  const t = useTranslations('aiSettings');
  const format = useFormatter();
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const profiles = useQuery({ queryKey: ['agent-profiles'], queryFn: getAgentProfiles });
  const settings = useQuery({ queryKey: ['ai-settings'], queryFn: () => apiRequest<AISettingsAliases>('/api/v1/settings/ai') });
  const sources = useInfiniteQuery({
    queryKey: ['agent-profile-sources'],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => {
      const query = new URLSearchParams({ limit: '100' });
      if (pageParam) query.set('cursor', pageParam);
      return apiRequest<SourcePage>(`/api/v1/sources?${query.toString()}`);
    },
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const [selectedId, setSelectedId] = React.useState('knowledge');
  const selected = profiles.data?.find((item) => item.id === selectedId);
  const [draft, setDraft] = React.useState<AgentProfile | null>(null);
  const [selectedRunId, setSelectedRunId] = React.useState<string | null>(null);
  const runs = useQuery({
    queryKey: ['agent-runs', selectedId],
    queryFn: () => listAgentRuns({ profile_id: selectedId, limit: 25 }),
    enabled: Boolean(selectedId),
    refetchInterval: (query) => query.state.data?.items.some((run) => ['queued', 'running', 'waiting_approval'].includes(run.status)) ? 3000 : false,
  });
  const runDetail = useQuery({
    queryKey: ['agent-run', selectedRunId],
    queryFn: () => getAgentRun(selectedRunId!),
    enabled: Boolean(selectedRunId),
    refetchInterval: (query) => query.state.data && ['queued', 'running', 'waiting_approval'].includes(query.state.data.status) ? 3000 : false,
  });
  React.useEffect(() => {
    setDraft(selected ? { ...selected, allowed_tools: [...selected.allowed_tools], source_ids: [...selected.source_ids] } : null);
    setSelectedRunId(null);
  }, [selected]);
  const save = useMutation({
    mutationFn: (value: AgentProfile) => updateAgentProfile(value, csrfToken),
    onSuccess: async (value) => {
      client.setQueryData<AgentProfile[]>(['agent-profiles'], (items) => items?.map((item) => item.id === value.id ? value : item));
      setDraft(value);
      await client.invalidateQueries({ queryKey: ['agent-runs', value.id] });
    },
  });
  const cancel = useMutation({
    mutationFn: (run: AgentRun) => cancelAgentRun(run.id, csrfToken),
    onSuccess: (value) => {
      client.setQueryData(['agent-run', value.id], value);
      void client.invalidateQueries({ queryKey: ['agent-runs', value.agent_id] });
    },
  });

  if (profiles.isLoading || settings.isLoading || sources.isLoading) return <p className="muted" role="status">{t('agentLoading')}</p>;
  if (profiles.isError || settings.isError || sources.isError || !draft) return <p className="error" role="alert">{t('agentLoadFailed')}</p>;
  const listedSources = (sources.data?.pages ?? []).flatMap((page) => page.items);
  const sourceOptions = new Map(listedSources.map((item) => [item.id, item]));
  for (const id of draft.source_ids) {
    if (!sourceOptions.has(id)) sourceOptions.set(id, { id, name: id, status: 'selected' });
  }
  const availableSources = [...sourceOptions.values()].filter((item) =>
    item.status === 'active' || draft.source_ids.includes(item.id),
  );
  const reasonKeys: Record<string, string> = {
    bounded_supervisor_handoff_unavailable: 'agentSupervisorHandoffUnavailable',
    phase_8_personal_tools_unavailable: 'agentPersonalToolsUnavailable',
    project_owner_tools_unavailable: 'agentProjectToolsUnavailable',
    news_owner_tools_unavailable: 'agentNewsToolsUnavailable',
    phase_8_planning_tools_unavailable: 'agentPlanningToolsUnavailable',
    phase_10_automation_unavailable: 'agentAutomationGate',
    bounded_browser_unavailable: 'agentBrowserUnavailable',
    model_alias_unconfigured: 'agentModelUnavailable',
    model_tool_capability_unverified: 'agentModelToolUnverified',
    registered_read_tools_unavailable: 'agentRegisteredToolsUnavailable',
  };
  const selectedTools = new Set(draft.allowed_tools.map((item) => item.name));
  const selectedSources = new Set(draft.source_ids);

  return (
    <section className="space-y-5 border-t border-border pt-6">
      <header className="space-y-1">
        <h2 className="text-lg font-semibold text-foreground">{t('agentsTitle')}</h2>
        <p className="text-sm text-muted-foreground">{t('agentsDescription')}</p>
      </header>
      <AgentList profiles={profiles.data ?? []} selectedId={selectedId} onSelect={setSelectedId} />
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.2fr)_minmax(18rem,.8fr)]">
        <form className="space-y-4 rounded-xl border border-border bg-surface p-4" onSubmit={(event) => { event.preventDefault(); if (draft) save.mutate(draft); }}>
          <h3 className="font-semibold text-foreground">{t(profileTitleKeys[draft.id] ?? 'agentProfileKnowledge')}</h3>
          {draft.unavailable_reasons.map((reason) => <p key={reason} className="text-sm text-muted-foreground">{t((reasonKeys[reason] ?? 'agentUnavailableReason') as 'agentUnavailableReason', { reason })}</p>)}
          <label className="flex items-center gap-2 text-sm text-foreground">
            <Checkbox checked={draft.enabled} onCheckedChange={(checked) => setDraft({ ...draft, enabled: checked === true })} />
            {t('agentEnabled')}
          </label>
          <label className="block space-y-1 text-sm">
            <span className="font-medium text-foreground">{t('agentModel')}</span>
            <Select value={draft.model_alias} onValueChange={(value) => setDraft({ ...draft, model_alias: value })}>
              <SelectTrigger aria-label={t('agentModel')}><SelectValue /></SelectTrigger>
              <SelectContent>{Object.entries(settings.data?.aliases ?? {}).map(([alias, mapping]) => (
                <SelectItem key={alias} value={alias} disabled={!mapping.model}>{alias}{mapping.model ? '' : ` · ${t('agentUnconfiguredAlias')}`}</SelectItem>
              ))}</SelectContent>
            </Select>
          </label>
          <label className="block space-y-1 text-sm">
            <span className="font-medium text-foreground">{t('agentPrompt')}</span>
            <Textarea maxLength={8000} value={draft.prompt} onChange={(event) => setDraft({ ...draft, prompt: event.target.value })} />
            <span className="text-xs text-muted-foreground">{draft.prompt.length} / 8000</span>
          </label>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium text-foreground">{t('agentTools')}</legend>
            {draft.available_tools.map((tool) => (
              <label key={`${tool.name}:${tool.version}:${tool.fingerprint}`} className="flex items-start gap-2 text-sm text-foreground">
                <Checkbox checked={selectedTools.has(tool.name)} onCheckedChange={(checked) => {
                  const next = new Map(draft.allowed_tools.map((item) => [item.name, item]));
                  if (checked === true) next.set(tool.name, tool); else next.delete(tool.name);
                  setDraft({ ...draft, allowed_tools: [...next.values()] });
                }} />
                <span>{tool.name} <span className="text-xs text-muted-foreground">v{tool.version}</span></span>
              </label>
            ))}
          </fieldset>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium text-foreground">{t('agentSources')}</legend>
            {availableSources.length ? availableSources.map((source) => (
              <label key={source.id} className="flex items-center gap-2 text-sm text-foreground">
                <Checkbox checked={selectedSources.has(source.id)} disabled={(source.status !== 'active' || selectedSources.size >= 32) && !selectedSources.has(source.id)} onCheckedChange={(checked) => {
                  const next = new Set(draft.source_ids);
                  if (checked === true && next.size < 32) next.add(source.id); else if (checked !== true) next.delete(source.id);
                  setDraft({ ...draft, source_ids: [...next] });
                }} />
                <span>{source.name}{source.status === 'unavailable' ? ` · ${t('agentSourceUnavailable')}` : ''}</span>
              </label>
            )) : <p className="text-sm text-muted-foreground">{t('agentNoSources')}</p>}
            {sources.hasNextPage && <Button type="button" className="secondary" disabled={sources.isFetchingNextPage} onClick={() => { void sources.fetchNextPage(); }}>{sources.isFetchingNextPage ? t('agentSourcesLoadingMore') : t('agentSourcesLoadMore')}</Button>}
            {sources.isFetchNextPageError && <p role="alert" className="text-sm text-destructive">{t('agentLoadFailed')}</p>}
          </fieldset>
          {save.error && <p role="alert" className="text-sm text-destructive">{t('agentSaveFailed')}</p>}
          {save.isSuccess && <p role="status" className="text-sm text-muted-foreground">{t('agentSaved')}</p>}
          <div className="flex gap-2">
            <Button type="submit" disabled={save.isPending || JSON.stringify(draft) === JSON.stringify(selected)}>{t('agentSave')}</Button>
            <Button type="button" className="secondary" disabled={save.isPending} onClick={() => setDraft(selected ? { ...selected, allowed_tools: [...selected.allowed_tools], source_ids: [...selected.source_ids] } : null)}>{t('agentCancel')}</Button>
          </div>
        </form>
        <aside className="space-y-3 rounded-xl border border-border bg-surface p-4">
          <h3 className="font-semibold text-foreground">{t('agentHistory')}</h3>
          <p className="text-xs text-muted-foreground">{t('agentHistoryBound')}</p>
          {runs.data?.items.length ? <p className="text-xs text-muted-foreground">{t('agentRunSummary', {
            total: runs.data.items.length,
            succeeded: runs.data.items.filter((run) => run.status === 'succeeded').length,
            failed: runs.data.items.filter((run) => run.status === 'failed').length,
          })}</p> : null}
          {runs.isError && <p role="alert" className="text-sm text-destructive">{t('agentLoadFailed')}</p>}
          {!runs.isError && !runs.data?.items.length && <p className="text-sm text-muted-foreground">{t('agentNoRuns')}</p>}
          <div className="space-y-2">
            {runs.data?.items.map((run) => (
              <Button key={run.id} type="button" className={`secondary h-auto w-full justify-start rounded-md p-2 text-left text-sm font-normal ${selectedRunId === run.id ? 'border-primary bg-accent/20' : 'border-border hover:bg-accent/10'}`} aria-pressed={selectedRunId === run.id} onClick={() => setSelectedRunId(run.id)}>
                <span className="font-medium text-foreground">{t(runStatusKeys[run.status] ?? 'agentRunFailed')}</span>
                <span className="ml-2 text-xs text-muted-foreground">{format.dateTime(new Date(run.created_at), { dateStyle: 'short', timeStyle: 'short' })}</span>
              </Button>
            ))}
          </div>
          {runDetail.data && <AgentRunDetail run={runDetail.data} onCancel={() => cancel.mutate(runDetail.data!)} cancelling={cancel.isPending} />}
        </aside>
      </div>
    </section>
  );
}
