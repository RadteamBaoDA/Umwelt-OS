'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { SettingsActions, SettingsPageHeader, SettingsSection } from './settings-layout';

type Entry = { key: string; value: string; default: string; customized: boolean };
type Prompts = { configuration_revision: number; prompts: Entry[] };
const MAX_CHARS = 4000;

/** Owner-editable system prompts with per-prompt reset; the server appends the fixed safety/format contract. */
export function SystemPromptsWorkspace() {
  const t = useTranslations('systemPrompts');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const query = useQuery({ queryKey: ['system-prompts'], queryFn: () => apiRequest<Prompts>('/api/v1/settings/prompts') });
  const save = useMutation({
    mutationFn: (value: Prompts) => apiRequest<Prompts>('/api/v1/settings/prompts', {
      method: 'PUT', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_revision: value.configuration_revision, prompts: draft }),
    }),
    onSuccess: (value) => { setDraft({}); client.setQueryData(['system-prompts'], value); },
  });
  const saved = query.data;
  const dirty = Object.keys(draft).length > 0;
  if (query.isPending) return <section className="content-panel skeleton" aria-label={t('loading')} />;
  if (query.isError || !saved) return <section className="content-panel"><h1>{t('unavailable')}</h1><p className="error">{t(apiFailureKey(query.error) ?? 'loadFailed')}</p><Button className="secondary" onClick={() => query.refetch()}>{t('retry')}</Button></section>;
  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('eyebrow')} title={t('title')} description={t('description')} status={<p className="m-0 text-sm text-muted-foreground" role="status">{save.isPending ? t('saving') : dirty ? t('unsaved') : t('saved')}</p>} />
    <p className="m-0 text-sm text-muted-foreground">{t('note')}</p>
    <form className="flex flex-col gap-5" onSubmit={(event) => { event.preventDefault(); save.mutate(saved); }}>
      {saved.prompts.map((entry) => {
        const value = draft[entry.key] ?? entry.value;
        const customized = value !== entry.default;
        const setValue = (next: string) => setDraft((current) => {
          const rest = { ...current };
          delete rest[entry.key];
          return next === entry.value ? rest : { ...rest, [entry.key]: next };
        });
        return <SettingsSection key={entry.key} title={<span className="flex flex-wrap items-center gap-2">{t(`label_${entry.key}` as 'label_chat')}{customized ? <span className="rounded-full border border-border px-2 py-0.5 text-xs font-normal text-muted-foreground">{t('customized')}</span> : null}</span>}>
          <label className="field">
            <span className="text-sm text-muted-foreground">{t(`hint_${entry.key}` as 'hint_chat')}</span>
            <Textarea rows={5} maxLength={MAX_CHARS} disabled={save.isPending} value={value} onChange={(event) => setValue(event.target.value)} />
          </label>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs text-muted-foreground">{t('characters', { count: value.length, max: MAX_CHARS })}</span>
            <Button type="button" className="secondary" disabled={save.isPending || !customized} onClick={() => setValue(entry.default)}>{t('reset')}</Button>
          </div>
        </SettingsSection>;
      })}
      {save.isError ? <p className="error" role="alert">{t(apiFailureKey(save.error) ?? 'saveFailed')}</p> : null}
      <SettingsActions>
        <Button type="button" className="secondary" disabled={save.isPending || !dirty} onClick={() => setDraft({})}>{t('cancelChanges')}</Button>
        <Button type="submit" disabled={save.isPending || !dirty || Object.values(draft).some((text) => !text.trim())}>{t('saveButton')}</Button>
      </SettingsActions>
    </form>
  </div>;
}
