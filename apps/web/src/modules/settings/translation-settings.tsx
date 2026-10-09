'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useWorkspace } from '@/core/workspace-context';
import { fetchTranslationSettings, saveTranslationSettings } from '@/modules/translations/api';
import type { TranslationLanguage } from '@/modules/translations/types';

/** Workspace translation toggle and target language. Owners save with expected_revision; members see it read only. */
export function TranslationSettings() {
  const t = useTranslations('translations');
  const { csrfToken } = useWorkspaceSession();
  const { isOwner, selection } = useWorkspace();
  const client = useQueryClient();
  const key = ['translation-settings', selection?.id ?? null];
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => fetchTranslationSettings(signal) });
  const [draft, setDraft] = useState<{ enabled?: boolean; language?: TranslationLanguage }>({});
  const enabled = draft.enabled ?? query.data?.enabled ?? false;
  const language = draft.language ?? query.data?.target_language ?? 'vi';
  const setEnabled = (value: boolean) => setDraft((d) => ({ ...d, enabled: value }));
  const setLanguage = (value: TranslationLanguage) => setDraft((d) => ({ ...d, language: value }));
  const save = useMutation({
    mutationFn: () => saveTranslationSettings(
      { enabled, target_language: language, expected_revision: query.data?.configuration_revision ?? 1 }, csrfToken),
    onSuccess: (value) => { client.setQueryData(key, value); setDraft({}); },
    onError: () => { setDraft({}); void query.refetch(); },
  });
  const dirty = Boolean(query.data && (query.data.enabled !== enabled || query.data.target_language !== language));
  const readOnly = !isOwner;
  const failure = save.error instanceof ApiError && save.error.status === 409 ? t('stale') : save.error ? t('saveFailed') : null;
  return (
    <section className="space-y-3 rounded-md border border-border p-3" aria-labelledby="translation-settings-title">
      <h3 id="translation-settings-title" className="font-semibold">{t('settingsTitle')}</h3>
      <p className="text-sm text-muted-foreground">{t('settingsHelp')}</p>
      {query.isError ? <p role="alert" className="text-sm text-destructive">{t('loadFailed')}</p> : null}
      <label className="flex min-h-11 items-center gap-2 text-sm">
        <Checkbox checked={enabled} disabled={readOnly || !query.data} onCheckedChange={(checked) => setEnabled(checked === true)} />
        {t('enable')}
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span>{t('targetLanguage')}</span>
        <Select value={language} disabled={readOnly || !query.data} onValueChange={(value) => setLanguage(value as TranslationLanguage)}>
          <SelectTrigger className="w-48"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="vi">{t('languageVi')}</SelectItem>
            <SelectItem value="en">{t('languageEn')}</SelectItem>
          </SelectContent>
        </Select>
      </label>
      {readOnly ? <p className="text-xs text-muted-foreground">{t('ownerOnly')}</p> : (
        <div className="flex items-center gap-3">
          <Button type="button" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>{t('save')}</Button>
          {save.isSuccess && !dirty ? <p role="status" className="text-xs text-muted-foreground">{t('saved')}</p> : null}
        </div>
      )}
      {failure ? <p role="alert" className="text-sm text-destructive">{failure}</p> : null}
    </section>
  );
}
