'use client';

import { SettingsActions, SettingsPageHeader } from '@/components/settings-layout';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import {
  getMemoryPrivacyConfig,
  purgeMemoryData,
  updateMemoryPrivacyConfig,
  memoryKeys,
  type MemoryPrivacyConfig,
  type MemoryPurgeRequest,
  type MemoryPurgeResponse,
} from '@/modules/knowledge';

/**
 * Renders owner memory and conversation privacy controls, auto-acceptance toggle,
 * and durable storage purge operations.
 */
export function MemoryPrivacySettings() {
  const s0 = useMemoryPrivacyState();
  const { t, tShell, ts, query, purgeMutation, draft, setDraft, purgeForgotten, purgeRejected, purgeHistory } = s0;

  if (query.isPending) {
    return (
      <section
        className="rounded-lg border bg-card p-6 skeleton h-48 rounded-lg"
        aria-label={ts('loadingMemoryPrivacy')}
      />
    );
  }

  if (query.isError) {
    return (
      <section className="space-y-4">
        <h1 className="text-xl font-semibold">{t('title')}</h1>
        <p className="text-destructive text-sm" role="alert">
          {query.error instanceof ApiError ? query.error.message : t('saveFailed')}
        </p>
        <Button variant="outline" onClick={() => query.refetch()}>
          Retry
        </Button>
      </section>
    );
  }

  const current: MemoryPrivacyConfig = draft ?? query.data ?? {
    store_conversation_history: true,
    store_agent_memory: false,
    auto_accept_memory: false,
  };

  const isDirty =
    draft !== null &&
    query.data !== undefined &&
    (draft.store_conversation_history !== query.data.store_conversation_history ||
      draft.store_agent_memory !== query.data.store_agent_memory ||
      draft.auto_accept_memory !== query.data.auto_accept_memory);

  const handleToggle = (key: keyof MemoryPrivacyConfig, checked: boolean) => {
    setDraft({ ...current, [key]: checked });
  };

  const handlePurge = () => {
    if (!purgeForgotten && !purgeRejected && !purgeHistory) return;
    if (window.confirm(t('purgeConfirm'))) {
      purgeMutation.mutate({
        purge_forgotten_memories: purgeForgotten,
        purge_rejected_candidates: purgeRejected,
        purge_conversation_history: purgeHistory,
      });
    }
  };

  const vm: MemoryPrivacySettingsVm = { ...s0, current, isDirty, handleToggle, handlePurge };
  return (
    <div className="flex flex-col gap-5">
      <SettingsPageHeader eyebrow={tShell('settings')} title={t('title')} description={t('subtitle')} />

      {/* Privacy Toggles */}
<PrivacyToggleForm vm={vm} />

      {/* Durable Purge Options */}
<PurgePanel vm={vm} />
    </div>
  );
}

type MemoryPrivacySettingsVm = ReturnType<typeof useMemoryPrivacyState> & { current: MemoryPrivacyConfig; isDirty: boolean; handleToggle: (key: keyof MemoryPrivacyConfig, checked: boolean) => void; handlePurge: () => void };

/** Storage toggles form with its save button and status. */
function PrivacyToggleForm({ vm }: { vm: MemoryPrivacySettingsVm }) {
  const { t, saveMutation, current, isDirty, handleToggle } = vm;
  return (
      <form
        onSubmit={(e) => {
          e.preventDefault();
          saveMutation.mutate(current);
        }}
        className="space-y-6"
      >
        <div className="space-y-4 border rounded-lg p-5 bg-card">
          <ToggleRow
            id="store-conversation-history"
            checked={current.store_conversation_history}
            title={t('conversationHistoryTitle')}
            description={t('conversationHistoryDesc')}
            onChange={(checked) => handleToggle('store_conversation_history', checked)}
          />
          <ToggleRow
            id="store-agent-memory"
            bordered
            checked={current.store_agent_memory}
            title={t('agentMemoryTitle')}
            description={t('agentMemoryDesc')}
            onChange={(checked) => handleToggle('store_agent_memory', checked)}
          />
          <ToggleRow
            id="auto-accept-memory"
            bordered
            checked={current.auto_accept_memory}
            disabled={!current.store_agent_memory}
            dimTitle={!current.store_agent_memory}
            title={t('autoAcceptTitle')}
            description={t('autoAcceptDesc')}
            onChange={(checked) => handleToggle('auto_accept_memory', checked)}
          />
        </div>

        {saveMutation.error && (
          <p className="text-destructive text-sm" role="alert">
            {saveMutation.error instanceof ApiError
              ? saveMutation.error.message
              : t('saveFailed')}
          </p>
        )}

        <SettingsActions>
          {saveMutation.isSuccess && !isDirty && (
            <span className="text-xs text-muted-foreground" role="status">
              {t('saved')}
            </span>
          )}
          <Button
            type="submit"
            disabled={saveMutation.isPending || !isDirty}
          >
            {saveMutation.isPending ? t('saving') : t('saveButton')}
          </Button>
        </SettingsActions>
      </form>
  );
}

/** Props of one labelled checkbox row in the privacy and purge lists. */
interface ToggleRowProps {
  id: string;
  checked: boolean;
  title: string;
  description: string;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  /** Adds the divider used for every row after the first. */
  bordered?: boolean;
  /** When defined, the title uses the dimming class variant (true dims it); the original markup only did this for the auto-accept row. */
  dimTitle?: boolean;
}

/** One checkbox row with a label and help text; markup matches the original hand-written rows. */
function ToggleRow({ id, checked, title, description, onChange, disabled, bordered, dimTitle }: ToggleRowProps) {
  return (
    <div className={bordered ? 'flex items-start space-x-3 pt-3 border-t border-border' : 'flex items-start space-x-3'}>
      <Checkbox
        id={id}
        checked={checked}
        disabled={disabled}
        onCheckedChange={(value) => onChange(Boolean(value))}
        className="mt-1"
      />
      <div className="space-y-1">
        <Label
          htmlFor={id}
          className={dimTitle === undefined ? 'font-medium cursor-pointer' : `font-medium cursor-pointer ${dimTitle ? 'opacity-50' : ''}`}
        >
          {title}
        </Label>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
    </div>
  );
}

/** Durable purge options with the confirmation-guarded purge button. */
function PurgePanel({ vm }: { vm: MemoryPrivacySettingsVm }) {
  const { t, purgeMutation, purgeForgotten, setPurgeForgotten, purgeRejected, setPurgeRejected, purgeHistory, setPurgeHistory, purgeResult, handlePurge } = vm;
  return (
      <div className="space-y-4 pt-6 border-t border-border">
        <div>
          <h2 className="text-lg font-semibold">{t('purgeTitle')}</h2>
          <p className="text-xs text-muted-foreground">
            {t('purgeHelp')}
          </p>
        </div>

        <div className="space-y-3 border rounded-lg p-5 bg-card">
          <ToggleRow id="purge-forgotten" checked={purgeForgotten} title={t('purgeForgotten')} description={t('purgeForgottenDesc')} onChange={setPurgeForgotten} />
          <ToggleRow id="purge-rejected" bordered checked={purgeRejected} title={t('purgeRejected')} description={t('purgeRejectedDesc')} onChange={setPurgeRejected} />
          <ToggleRow id="purge-history" bordered checked={purgeHistory} title={t('purgeHistory')} description={t('purgeHistoryDesc')} onChange={setPurgeHistory} />
        </div>

        {purgeMutation.error && (
          <p className="text-destructive text-sm" role="alert">
            {purgeMutation.error instanceof ApiError
              ? purgeMutation.error.message
              : t('purgeFailed')}
          </p>
        )}

        {purgeResult && (
          <p className="text-sm text-foreground bg-secondary/50 p-3 rounded-md" role="status">
            {t('purgeSuccess', {
              memories: purgeResult.purged_memories_count,
              candidates: purgeResult.purged_candidates_count,
              conversations: purgeResult.purged_conversations_count,
            })}
          </p>
        )}

        <Button
          type="button"
          variant="outline" className="text-destructive hover:bg-destructive/10 hover:text-destructive border-destructive/30"
          disabled={
            purgeMutation.isPending ||
            (!purgeForgotten && !purgeRejected && !purgeHistory)
          }
          onClick={handlePurge}
        >
          {purgeMutation.isPending ? t('saving') : t('purgeButton')}
        </Button>
      </div>
  );
}

/** Loads the privacy configuration and declares the save and purge mutations with their local draft and purge selections. */
function useMemoryPrivacyState() {
  const t = useTranslations('memoryPrivacy');
  const tShell = useTranslations('shell');
  const ts = useTranslations('uiSweep');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: memoryKeys.privacy,
    queryFn: getMemoryPrivacyConfig,
  });

  const [draft, setDraft] = useState<MemoryPrivacyConfig | null>(null);
  const [purgeForgotten, setPurgeForgotten] = useState(false);
  const [purgeRejected, setPurgeRejected] = useState(false);
  const [purgeHistory, setPurgeHistory] = useState(false);
  const [purgeResult, setPurgeResult] = useState<MemoryPurgeResponse | null>(null);

  const saveMutation = useMutation({
    mutationFn: (config: MemoryPrivacyConfig) =>
      updateMemoryPrivacyConfig(config, csrfToken),
    onSuccess: (updated) => {
      setDraft(updated);
      queryClient.setQueryData(memoryKeys.privacy, updated);
    },
  });

  const purgeMutation = useMutation({
    mutationFn: (payload: MemoryPurgeRequest) =>
      purgeMemoryData(payload, csrfToken),
    onSuccess: (result) => {
      setPurgeResult(result);
      setPurgeForgotten(false);
      setPurgeRejected(false);
      setPurgeHistory(false);
      queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });

  return { t, tShell, ts, queryClient, query, saveMutation, purgeMutation, draft, setDraft, purgeForgotten, setPurgeForgotten, purgeRejected, setPurgeRejected, purgeHistory, setPurgeHistory, purgeResult, setPurgeResult, csrfToken };
}
