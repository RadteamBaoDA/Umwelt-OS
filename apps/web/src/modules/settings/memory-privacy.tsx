'use client';

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
} from '@/modules/knowledge/api';

/**
 * Renders owner memory and conversation privacy controls, auto-acceptance toggle,
 * and durable storage purge operations.
 */
export function MemoryPrivacySettings() {
  const t = useTranslations('memoryPrivacy');
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

  if (query.isPending) {
    return (
      <section
        className="content-panel skeleton h-48 rounded-lg"
        aria-label="Loading memory privacy settings"
      />
    );
  }

  if (query.isError) {
    return (
      <section className="content-panel space-y-4">
        <h1 className="text-xl font-bold">{t('title')}</h1>
        <p className="text-destructive text-sm" role="alert">
          {query.error instanceof ApiError ? query.error.message : t('saveFailed')}
        </p>
        <Button className="secondary" onClick={() => query.refetch()}>
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

  return (
    <section className="content-panel space-y-8 max-w-3xl">
      <div className="section-heading">
        <div>
          <span className="brand">Local first</span>
          <h1 className="text-2xl font-bold tracking-tight">{t('title')}</h1>
          <p className="muted text-sm text-muted-foreground mt-1">{t('subtitle')}</p>
        </div>
      </div>

      {/* Privacy Toggles */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          saveMutation.mutate(current);
        }}
        className="space-y-6"
      >
        <div className="space-y-4 border rounded-lg p-5 bg-card">
          {/* Conversation History Storage */}
          <div className="flex items-start space-x-3">
            <Checkbox
              id="store-conversation-history"
              checked={current.store_conversation_history}
              onCheckedChange={(checked) =>
                handleToggle('store_conversation_history', Boolean(checked))
              }
              className="mt-1"
            />
            <div className="space-y-1">
              <Label
                htmlFor="store-conversation-history"
                className="font-medium cursor-pointer"
              >
                {t('conversationHistoryTitle')}
              </Label>
              <p className="text-xs text-muted-foreground">
                {t('conversationHistoryDesc')}
              </p>
            </div>
          </div>

          {/* Agent Memory Storage */}
          <div className="flex items-start space-x-3 pt-3 border-t border-border">
            <Checkbox
              id="store-agent-memory"
              checked={current.store_agent_memory}
              onCheckedChange={(checked) =>
                handleToggle('store_agent_memory', Boolean(checked))
              }
              className="mt-1"
            />
            <div className="space-y-1">
              <Label
                htmlFor="store-agent-memory"
                className="font-medium cursor-pointer"
              >
                {t('agentMemoryTitle')}
              </Label>
              <p className="text-xs text-muted-foreground">
                {t('agentMemoryDesc')}
              </p>
            </div>
          </div>

          {/* Auto-accept Memory Suggestions */}
          <div className="flex items-start space-x-3 pt-3 border-t border-border">
            <Checkbox
              id="auto-accept-memory"
              checked={current.auto_accept_memory}
              disabled={!current.store_agent_memory}
              onCheckedChange={(checked) =>
                handleToggle('auto_accept_memory', Boolean(checked))
              }
              className="mt-1"
            />
            <div className="space-y-1">
              <Label
                htmlFor="auto-accept-memory"
                className={`font-medium cursor-pointer ${
                  !current.store_agent_memory ? 'opacity-50' : ''
                }`}
              >
                {t('autoAcceptTitle')}
              </Label>
              <p className="text-xs text-muted-foreground">
                {t('autoAcceptDesc')}
              </p>
            </div>
          </div>
        </div>

        {saveMutation.error && (
          <p className="text-destructive text-sm" role="alert">
            {saveMutation.error instanceof ApiError
              ? saveMutation.error.message
              : t('saveFailed')}
          </p>
        )}

        <div className="flex items-center gap-3">
          <Button
            type="submit"
            disabled={saveMutation.isPending || !isDirty}
          >
            {saveMutation.isPending ? t('saving') : t('saveButton')}
          </Button>
          {saveMutation.isSuccess && !isDirty && (
            <span className="text-xs text-muted-foreground" role="status">
              {t('saved')}
            </span>
          )}
        </div>
      </form>

      {/* Durable Purge Options */}
      <div className="space-y-4 pt-6 border-t border-border">
        <div>
          <h2 className="text-lg font-semibold">{t('purgeTitle')}</h2>
          <p className="text-xs text-muted-foreground">
            Durable deletion operations permanently erase records from the system.
          </p>
        </div>

        <div className="space-y-3 border rounded-lg p-5 bg-card">
          <div className="flex items-start space-x-3">
            <Checkbox
              id="purge-forgotten"
              checked={purgeForgotten}
              onCheckedChange={(checked) => setPurgeForgotten(Boolean(checked))}
              className="mt-1"
            />
            <div className="space-y-1">
              <Label htmlFor="purge-forgotten" className="font-medium cursor-pointer">
                {t('purgeForgotten')}
              </Label>
              <p className="text-xs text-muted-foreground">{t('purgeForgottenDesc')}</p>
            </div>
          </div>

          <div className="flex items-start space-x-3 pt-3 border-t border-border">
            <Checkbox
              id="purge-rejected"
              checked={purgeRejected}
              onCheckedChange={(checked) => setPurgeRejected(Boolean(checked))}
              className="mt-1"
            />
            <div className="space-y-1">
              <Label htmlFor="purge-rejected" className="font-medium cursor-pointer">
                {t('purgeRejected')}
              </Label>
              <p className="text-xs text-muted-foreground">{t('purgeRejectedDesc')}</p>
            </div>
          </div>

          <div className="flex items-start space-x-3 pt-3 border-t border-border">
            <Checkbox
              id="purge-history"
              checked={purgeHistory}
              onCheckedChange={(checked) => setPurgeHistory(Boolean(checked))}
              className="mt-1"
            />
            <div className="space-y-1">
              <Label htmlFor="purge-history" className="font-medium cursor-pointer">
                {t('purgeHistory')}
              </Label>
              <p className="text-xs text-muted-foreground">{t('purgeHistoryDesc')}</p>
            </div>
          </div>
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
          className="secondary text-destructive hover:bg-destructive/10 hover:text-destructive border-destructive/30"
          disabled={
            purgeMutation.isPending ||
            (!purgeForgotten && !purgeRejected && !purgeHistory)
          }
          onClick={handlePurge}
        >
          {purgeMutation.isPending ? t('saving') : t('purgeButton')}
        </Button>
      </div>
    </section>
  );
}
